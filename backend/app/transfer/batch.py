"""TransferBatch registry: RAM-only grouping of project-sync transfer jobs.

Phase 4D: a batch is a pure grouping record — (project, target server, job
ids) — over real TransferJobs owned by TransferService. The registry never
copies data and never imports the transfer service: the API layer injects a
``create_job`` callback at start and feeds job states into every read, so the
registry stays a plain data structure. State and counts are always DERIVED
from the underlying jobs, never stored as truth. Batches are never persisted;
a restart forgets them while recorded placements survive in workspace.json.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import NotFoundError
from app.models.distribution import ArtifactSyncItem
from app.models.transfer import TransferBatch, TransferBatchState, TransferState

__all__ = ["BatchRegistry", "CreateJobCallback"]

CreateJobCallback = Callable[[ArtifactSyncItem], str]
# The registry exposes a `list` METHOD (pinned API), which shadows the builtin
# inside class scope; aliases keep the builtin list usable in annotations.
BatchList = list[TransferBatch]
JobIds = list[str]
SyncItems = list[ArtifactSyncItem]

_TERMINAL_STATES = frozenset(
    {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
)
_IN_FLIGHT_STATES = frozenset(
    {TransferState.PLANNING, TransferState.RUNNING, TransferState.VERIFYING}
)


@dataclass(slots=True)
class _Record:
    """Immutable batch skeleton; every view is derived fresh from job states."""

    batch_id: str
    project_id: str
    target_server_id: str
    job_ids: list[str]
    created_at: str


def _all_terminal(job_ids: list[str], job_states: dict[str, TransferState]) -> bool:
    # Unknown job ids count as QUEUED (spec: missing state -> QUEUED), so a
    # batch whose jobs vanished from the transfer history is not terminal.
    return all(
        job_states.get(job_id, TransferState.QUEUED) in _TERMINAL_STATES for job_id in job_ids
    )


def _derive_state(
    *, queued: int, running: int, completed: int, failed: int, cancelled: int
) -> TransferBatchState:
    """Batch state from per-job counts (no storage of derived state).

    Empty batch (nothing to transfer) -> COMPLETED. Any in-flight work ->
    RUNNING; QUEUED only while nothing has started or finished yet. Terminal
    mixes: any success beside a failure or cancellation -> PARTIAL_FAILED;
    failures/cancellations without successes -> FAILED (all cancelled alone ->
    CANCELLED); everything completed -> COMPLETED.
    """
    if queued + running + completed + failed + cancelled == 0:
        return TransferBatchState.COMPLETED
    if running > 0 or (queued > 0 and completed + failed + cancelled > 0):
        return TransferBatchState.RUNNING
    if queued > 0:
        return TransferBatchState.QUEUED
    if failed > 0:
        return TransferBatchState.PARTIAL_FAILED if completed > 0 else TransferBatchState.FAILED
    if cancelled > 0:
        return TransferBatchState.PARTIAL_FAILED if completed > 0 else TransferBatchState.CANCELLED
    return TransferBatchState.COMPLETED


class BatchRegistry:
    """Bounded in-RAM registry of transfer batches (JobRegistry style)."""

    def __init__(self, history_limit: int = 50) -> None:
        self._active: dict[str, _Record] = {}
        self._history: deque[_Record] = deque(maxlen=history_limit)

    # ---- creation ------------------------------------------------------------------

    async def start(
        self,
        *,
        project_id: str,
        target_server_id: str,
        transfer_items: SyncItems,
        create_job: CreateJobCallback,
    ) -> TransferBatch:
        """Create one job per TRANSFER item via the injected callback.

        The callback is the API layer's adapter onto TransferService.create;
        the registry itself never touches the service. If a later create
        fails, the jobs already created stay visible under a registered batch
        instead of being orphaned; the error still propagates to the caller.
        """
        job_ids: list[str] = []
        try:
            for item in transfer_items:
                job_ids.append(create_job(item))
        except Exception:
            if job_ids:
                self._register(project_id, target_server_id, job_ids)
            raise
        record = self._register(project_id, target_server_id, job_ids)
        # Jobs were just queued and no loop turn has run: unknown states count
        # as QUEUED, so a fresh batch is QUEUED (COMPLETED for an empty one).
        return self._view(record, {})

    # ---- read model (derive, and archive finished batches lazily) -------------------

    def get(self, batch_id: str, job_states: dict[str, TransferState]) -> TransferBatch:
        self._archive_finished(job_states)
        record = self._active.get(batch_id) or next(
            (h for h in self._history if h.batch_id == batch_id), None
        )
        if record is None:
            raise NotFoundError(f"transfer batch {batch_id!r} not found")
        return self._view(record, job_states)

    def list(self, job_states: dict[str, TransferState]) -> BatchList:
        self._archive_finished(job_states)
        views = [self._view(record, job_states) for record in reversed(self._active.values())]
        views.extend(self._view(record, job_states) for record in self._history)
        return views  # active (newest first) + history (newest first)

    # ---- internals -------------------------------------------------------------------

    def _register(self, project_id: str, target_server_id: str, job_ids: JobIds) -> _Record:
        record = _Record(
            batch_id=uuid.uuid4().hex[:12],
            project_id=project_id,
            target_server_id=target_server_id,
            job_ids=list(job_ids),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._active[record.batch_id] = record
        return record

    def _archive_finished(self, job_states: dict[str, TransferState]) -> None:
        """Lazy archive: read-time only, bounded history deque holds the rest."""
        finished = [
            batch_id
            for batch_id, record in self._active.items()
            if _all_terminal(record.job_ids, job_states)
        ]
        for batch_id in finished:
            self._history.appendleft(self._active.pop(batch_id))

    def _view(self, record: _Record, job_states: dict[str, TransferState]) -> TransferBatch:
        states = [job_states.get(job_id, TransferState.QUEUED) for job_id in record.job_ids]
        queued = sum(1 for state in states if state is TransferState.QUEUED)
        running = sum(1 for state in states if state in _IN_FLIGHT_STATES)
        completed = sum(1 for state in states if state is TransferState.COMPLETED)
        failed = sum(1 for state in states if state is TransferState.FAILED)
        cancelled = sum(1 for state in states if state is TransferState.CANCELLED)
        return TransferBatch(
            batch_id=record.batch_id,
            project_id=record.project_id,
            target_server_id=record.target_server_id,
            job_ids=list(record.job_ids),
            created_at=record.created_at,
            state=_derive_state(
                queued=queued,
                running=running,
                completed=completed,
                failed=failed,
                cancelled=cancelled,
            ),
            total_jobs=len(record.job_ids),
            queued_jobs=queued,
            running_jobs=running,
            completed_jobs=completed,
            failed_jobs=failed,
            cancelled_jobs=cancelled,
        )

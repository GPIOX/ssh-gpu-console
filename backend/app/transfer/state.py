"""Transfer job registry: RAM only, bounded, never persisted.

Active jobs are keyed by job_id with their worker tasks; completed/failed
history is a bounded deque. The service owns state transitions.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime

from app.core.errors import ConflictError
from app.models.transfer import TransferJob, TransferState, TransferStrategy


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobRegistry:
    """Bounded in-RAM registry of transfer jobs."""

    def __init__(self, history_limit: int = 100) -> None:
        self._active: dict[str, TransferJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._history: deque[TransferJob] = deque(maxlen=history_limit)
        self._lock = asyncio.Lock()

    def create(
        self,
        *,
        artifact_id: str,
        artifact_label: str,
        source_server_id: str,
        source_path: str,
        target_server_id: str,
        target_path: str,
        strategy_requested: TransferStrategy,
    ) -> TransferJob:
        job = TransferJob(
            job_id=uuid.uuid4().hex[:12],
            artifact_id=artifact_id,
            artifact_label=artifact_label,
            source_server_id=source_server_id,
            source_path=source_path,
            target_server_id=target_server_id,
            target_path=target_path,
            strategy_requested=strategy_requested,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._active[job.job_id] = job
        return job

    def get(self, job_id: str) -> TransferJob:
        job = self._active.get(job_id)
        if job is None:
            raise ConflictError(f"transfer job {job_id!r} is not active")
        return job

    def find(self, job_id: str) -> TransferJob | None:
        return self._active.get(job_id) or next(
            (h for h in self._history if h.job_id == job_id), None
        )

    def transition(self, job: TransferJob, state: TransferState) -> None:
        job.state = state
        if state in (TransferState.RUNNING,):
            _stamp(job, "started_at")
        if state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED):
            _stamp(job, "finished_at")
            self._archive(job)

    def _archive(self, job: TransferJob) -> None:
        self._active.pop(job.job_id, None)
        self._history.appendleft(job)

    def fail(self, job: TransferJob, code: str, message: str) -> None:
        job.error_code = code
        job.error_message = message[:300]
        self.transition(job, TransferState.FAILED)

    def cancel_requested(self, job: TransferJob) -> None:
        if job.state not in (
            TransferState.QUEUED,
            TransferState.PLANNING,
            TransferState.RUNNING,
            TransferState.VERIFYING,
        ):
            raise ConflictError(f"job {job.job_id} is not cancellable in state {job.state}")

    def bind_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[job_id] = task

    def unbind_task(self, job_id: str) -> asyncio.Task[None] | None:
        return self._tasks.pop(job_id, None)

    def active_jobs(self) -> list[TransferJob]:
        return list(self._active.values())

    def history(self) -> list[TransferJob]:
        return list(self._history)

    def all_jobs(self) -> list[TransferJob]:
        return [*self.active_snapshot(), *self.history()]

    def active_snapshot(self) -> list[TransferJob]:
        return list(self._active.values())

    def active_count(self) -> int:
        return len(self._active)

    def cancel_all(self) -> None:
        for job in self._active.values():
            if job.state in (
                TransferState.QUEUED,
                TransferState.PLANNING,
                TransferState.RUNNING,
                TransferState.VERIFYING,
            ):
                self.transition(job, TransferState.CANCELLED)


def _stamp(job: TransferJob, field: str) -> None:
    setattr(job, field, datetime.now(UTC).isoformat(timespec="seconds"))

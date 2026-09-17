"""Transfer job registry: RAM only, bounded, never persisted.

Active jobs are keyed by job_id with their worker tasks; completed/failed
history is a bounded deque. The service owns state transitions. A target lock
(one active job per (target_server_id, normalized target_path)) keeps two jobs
from concurrently writing the same target directory.
"""

from __future__ import annotations

import asyncio
import posixpath
import uuid
from collections import deque
from datetime import UTC, datetime

from app.core.errors import ConflictError
from app.models.transfer import TransferJob, TransferState, TransferStrategy


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _target_key(server_id: str, path: str) -> tuple[str, str]:
    """Lock key for a transfer target: the same directory under differently
    written paths (trailing slash, redundant segments) must be ONE lock."""
    return server_id, posixpath.normpath(path).rstrip("/") or "/"


class JobRegistry:
    """Bounded in-RAM registry of transfer jobs."""

    def __init__(self, history_limit: int = 100) -> None:
        self._active: dict[str, TransferJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._history: deque[TransferJob] = deque(maxlen=history_limit)
        self._target_locks: dict[tuple[str, str], str] = {}
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
        excludes: list[str] | None = None,
        immutable: bool = False,
    ) -> TransferJob:
        key = _target_key(target_server_id, target_path)
        if key in self._target_locks:
            raise ConflictError(
                f"target {target_server_id}:{key[1]} already has an active transfer;"
                " wait for it to finish or choose another target path"
            )
        job = TransferJob(
            job_id=uuid.uuid4().hex[:12],
            artifact_id=artifact_id,
            artifact_label=artifact_label,
            source_server_id=source_server_id,
            source_path=source_path,
            target_server_id=target_server_id,
            target_path=target_path,
            strategy_requested=strategy_requested,
            excludes=list(excludes or []),
            immutable=immutable,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._target_locks[key] = job.job_id
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
            self._release_target_lock(job)
            self._archive(job)

    def _release_target_lock(self, job: TransferJob) -> None:
        key = _target_key(job.target_server_id, job.target_path)
        if self._target_locks.get(key) == job.job_id:
            del self._target_locks[key]

    def target_lock_holder(self, server_id: str, path: str) -> str | None:
        """Job id currently holding the target lock for (server_id, path)."""
        return self._target_locks.get(_target_key(server_id, path))

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

    def clear_history(self) -> int:
        """Drop every terminal-state entry from history; active jobs stay."""
        terminal = (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED)
        kept = [job for job in self._history if job.state not in terminal]
        removed = len(self._history) - len(kept)
        if removed:
            self._history.clear()
            self._history.extend(kept)
        return removed

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

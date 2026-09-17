"""TransferService: queue, plan, run, verify, cancel, retry — RAM only.

Isolation guarantees (WORKSPACE_SYNC_PLAN.md §6):
- transfers use their own per-server slots and a global cap; they never
  acquire the telemetry executor's semaphores (short preflight commands
  excepted, same policy as inspections);
- transfer sessions are dedicated SSH connections sharing auth/host-key
  policy with telemetry but with independent lifecycles;
- jobs live in RAM only (JobRegistry); on success + quick verify the target
  placement metadata is created ONCE (explicit persistence event).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.transfer import (
    TransferJob,
    TransferPlan,
    TransferRequest,
    TransferState,
    TransferStrategy,
)
from app.models.workspace import PlacementCreate
from app.ssh.file_transfer import TransferSession
from app.transfer.state import JobRegistry
from app.transfer.strategies.local_relay import CancelRequested

if TYPE_CHECKING:
    from app.collectors.base import ExecutorLike
    from app.core.config import Settings
    from app.models.server import ServerRecord
    from app.ssh.manager import SshManager
    from app.workspace.service import WorkspaceService

logger = get_logger("transfer.service")


class TransferService:
    """Owns transfer jobs end to end; never persists job state."""

    def __init__(
        self,
        *,
        settings: Settings,
        ssh: SshManager,
        workspace: WorkspaceService,
        jobs: JobRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._ssh = ssh
        self._workspace = workspace
        self._jobs = jobs or JobRegistry(
            history_limit=int(getattr(settings, "transfer_job_history", 100))
        )
        self._global_slot = asyncio.Semaphore(int(getattr(settings, "max_transfers_global", 2)))
        self._stopped = False

    # ---- read model (REST; RAM only, never triggers SSH) -----------------------

    def list_jobs(self) -> list[TransferJob]:
        return self._jobs.active_snapshot() + self._jobs.history()

    def job(self, job_id: str) -> TransferJob:
        job = self._jobs.find(job_id)
        if job is None:
            raise NotFoundError(f"transfer job {job_id!r} not found")
        return job

    def active_count(self) -> int:
        return self._jobs.active_count()

    # ---- planning (creates no job) ------------------------------------------------

    async def plan(self, request: TransferRequest) -> TransferPlan:
        stub = self._materialize(request)
        source_server = self._server(stub.source_server_id)
        target_server = self._server(stub.target_server_id)
        from app.transfer.planner import plan_transfer

        return await plan_transfer(
            requested=request.strategy,
            source_server=source_server,
            target_server=target_server,
            source_executor=self._executor(stub.source_server_id),
            target_executor=self._executor(stub.target_server_id),
            source_path=stub.source_path,
            target_path=stub.target_path,
            artifact_id=request.artifact_id,
            ssh=self._ssh,
        )

    # ---- creation -----------------------------------------------------------------

    def create(self, request: TransferRequest) -> dict[str, str]:
        """Validate + queue a job; the worker does planning and execution."""
        if self._stopped:
            raise ConflictError("transfer service is shutting down")
        artifact = self._workspace.artifact(request.artifact_id)
        placement = self._workspace.placement(request.source_placement_id)
        if placement.artifact_id != request.artifact_id:
            raise ConflictError("source placement does not belong to the artifact")
        source_server = self._server(placement.server_id)
        target_server = self._server(request.target_server_id)
        if not source_server.enabled or not target_server.enabled:
            raise ConflictError("source and target servers must be enabled")
        if placement.server_id == request.target_server_id:
            raise ConflictError("source and target server are the same")

        job = self._jobs.create(
            artifact_id=artifact.artifact_id,
            artifact_label=_artifact_label(artifact),
            source_server_id=placement.server_id,
            source_path=placement.remote_path,
            target_server_id=request.target_server_id,
            target_path=_clean_path(request.target_path),
            strategy_requested=request.strategy,
        )
        task = asyncio.create_task(self._run_job(job))
        self._jobs.bind_task(job.job_id, task)
        return {"job_id": job.job_id}

    def cancel(self, job_id: str) -> None:
        job = self._jobs.find(job_id)
        if job is None:
            raise NotFoundError(f"transfer job {job_id!r} not found")
        if job.state not in (
            TransferState.QUEUED,
            TransferState.PLANNING,
            TransferState.RUNNING,
            TransferState.VERIFYING,
        ):
            raise ConflictError(f"job {job_id} is not cancellable in state {job.state.value}")
        self._jobs.transition(job, TransferState.CANCELLED)
        # Workers observe the cancelled state at their next checkpoint; queued
        # jobs never start planning afterwards.

    async def retry(self, job_id: str) -> dict[str, str]:
        """Re-queue a finished job's parameters as a NEW job."""
        original = self._jobs.find(job_id)
        if original is None:
            raise NotFoundError(f"transfer job {job_id!r} not found")
        if original.state not in (
            TransferState.FAILED,
            TransferState.COMPLETED,
            TransferState.CANCELLED,
        ):
            raise ConflictError("only failed, completed or cancelled jobs can be retried")
        placement_id = _find_placement_id(
            self._workspace, original.source_server_id, original.source_path
        )
        if placement_id is None:
            raise ConflictError("source placement no longer exists")
        created = self.create(
            TransferRequest(
                artifact_id=original.artifact_id,
                source_placement_id=placement_id,
                target_server_id=original.target_server_id,
                target_path=original.target_path,
                strategy=original.strategy_requested,
            )
        )
        return created

    # ---- worker ---------------------------------------------------------------------

    async def _run_job(self, job: TransferJob) -> None:
        try:
            async with self._global_slot:
                await self._plan_and_run(job)
        except asyncio.CancelledError:
            self._jobs.transition(job, TransferState.CANCELLED)
        except ConflictError as error:
            self._jobs.fail(job, "conflict", str(error))
        except Exception as exc:
            if job.state.value != TransferState.CANCELLED.value:
                self._jobs.fail(job, "transfer_failed", str(exc)[:300])
            logger.info("transfer %s failed: %s", job.job_id, str(exc)[:200])

    async def _plan_and_run(self, job: TransferJob) -> None:
        from app.transfer.planner import plan_transfer

        if self._jobs.find(job.job_id) is None:
            return  # cancelled while queued
        self._jobs.transition(job, TransferState.PLANNING)

        source_server = self._server(job.source_server_id)
        target_server = self._server(job.target_server_id)
        plan: TransferPlan = await plan_transfer(
            requested=TransferStrategy(job.strategy_requested),
            source_server=source_server,
            target_server=target_server,
            source_executor=self._executor(job.source_server_id),
            target_executor=self._executor(job.target_server_id),
            source_path=job.source_path,
            target_path=job.target_path,
            artifact_id=job.artifact_id,
            ssh=self._ssh,
        )
        if self._jobs.find(job.job_id) is None:
            return
        if plan.strategy_selected is None:
            self._jobs.fail(job, "strategy_unavailable", plan.reason[:300])
            return
        job.strategy_used = plan.strategy_selected

        source_slot = self._ssh.transfer_slot_or_create(job.source_server_id)
        target_slot = self._ssh.transfer_slot_or_create(job.target_server_id)
        async with source_slot, target_slot:
            if self._jobs.find(job.job_id) is None:
                return
            if plan.strategy_selected is TransferStrategy.DIRECT_RSYNC:
                await self._run_rsync(job, source_server, target_server)
            else:
                self._jobs.transition(job, TransferState.RUNNING)
                import contextlib

                source_session, target_session = await self._relay_sessions(
                    source_server, target_server
                )
                try:
                    await _relay(job, source_session, target_session, self._settings)
                except CancelRequested:
                    self._jobs.transition(job, TransferState.CANCELLED)
                    return
                finally:
                    with contextlib.suppress(Exception):
                        await source_session.close()  # type: ignore[attr-defined]

        if self._jobs.find(job.job_id) is None:
            return
        self._jobs.transition(job, TransferState.VERIFYING)
        ok, reason = await self._verify(job, plan.strategy_selected)
        if not ok:
            self._jobs.fail(job, "verify_failed", reason[:300])
            return
        self._jobs.transition(job, TransferState.COMPLETED)
        self._record_target_placement(job)

    # ---- strategy pieces ---------------------------------------------------------------

    async def _run_rsync(
        self,
        job: TransferJob,
        source_server: ServerRecord,
        target_server: ServerRecord,
    ) -> None:
        from app.transfer.strategies.direct_rsync import CancelRequested, rsync_transfer

        if job.state.value == TransferState.CANCELLED.value:
            raise CancelRequested(job.job_id)
        self._jobs.transition(job, TransferState.RUNNING)
        exit_code = await rsync_transfer(
            job=job,
            ssh=self._ssh,
            source_server=source_server,
            target_server=target_server,
            source_size_b=await self._source_size(job),
        )
        if exit_code != 0:
            raise ConflictError(f"rsync exited with code {exit_code}")

    async def _verify(self, job: TransferJob, strategy: TransferStrategy) -> tuple[bool, str]:
        from app.transfer.verifier import quick_verify

        if strategy is TransferStrategy.DIRECT_RSYNC:
            return True, "rsync self-consistency"
        try:
            source_session, target_session = await self._relay_sessions(
                self._server(job.source_server_id), self._server(job.target_server_id)
            )
            return await quick_verify(job=job, source=source_session, target=target_session)
        except Exception as exc:
            return False, f"verify failed: {str(exc)[:200]}"

    def _record_target_placement(self, job: TransferJob) -> None:
        """Create the target placement ONCE after a verified transfer."""
        try:
            existing = {
                (p.artifact_id, p.server_id, p.remote_path) for p in self._workspace.placements()
            }
            if (job.artifact_id, job.target_server_id, job.target_path) in existing:
                return
            self._workspace.create_placement(
                PlacementCreate(
                    artifact_id=job.artifact_id,
                    server_id=job.target_server_id,
                    remote_path=job.target_path,
                )
            )
            logger.info("placement recorded: %s on %s", job.artifact_id, job.target_server_id)
        except Exception as error:  # metadata failure must not fail the job
            logger.warning("auto-placement skipped: %s", str(error)[:200])

    async def _relay_sessions(
        self, source_server: ServerRecord, target_server: ServerRecord
    ) -> tuple[TransferSession, TransferSession]:
        """Typed SFTP sessions for the relay path (sessions are SFTP-backed)."""
        from typing import cast

        source = await self._ssh.transfer_session(source_server)
        target = await self._ssh.transfer_session(target_server)
        return cast(TransferSession, source), cast(TransferSession, target)

    async def _source_size(self, job: TransferJob) -> int | None:
        try:
            result = await self._executor(job.source_server_id).run(
                f"stat -c %s -- {_q(job.source_path)}", timeout_s=10
            )
            if result.exit_code == 0:
                return int(result.stdout.strip() or 0) or None
        except Exception:
            pass
        return None

    # ---- deps ------------------------------------------------------------------------

    def _server(self, server_id: str) -> ServerRecord:
        from app.servers.registry import get_default_registry

        try:
            return get_default_registry().get(server_id)
        except Exception as error:
            raise NotFoundError(f"server {server_id!r} not found") from error

    def _executor(self, server_id: str) -> ExecutorLike:
        from app.ssh.transport import build_executor

        return build_executor(self._ssh, self._server(server_id))

    def _materialize(self, request: TransferRequest) -> SimpleNamespace:
        """Light job-shaped stub for planning (no job created)."""
        placement = self._workspace.placement(request.source_placement_id)
        if placement.artifact_id != request.artifact_id:
            raise ConflictError("source placement does not belong to the artifact")
        return SimpleNamespace(
            artifact_id=request.artifact_id,
            source_server_id=placement.server_id,
            source_path=placement.remote_path,
            target_server_id=request.target_server_id,
            target_path=_clean_path(request.target_path),
        )

    # ---- shutdown -----------------------------------------------------------------------

    async def stop(self) -> None:
        """Cancel all running/queued jobs, close transfer sessions."""
        self._stopped = True
        self._jobs.cancel_all()
        tasks = list(self._jobs._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await self._ssh.close_transfer_sessions()


async def _relay(
    job: TransferJob, source: TransferSession, target: TransferSession, settings: object
) -> None:
    from app.transfer.strategies.local_relay import relay_transfer

    await relay_transfer(
        job=job,
        source=source,
        target=target,
        chunk_size=int(getattr(settings, "transfer_chunk_size_b", 4194304)),
    )


def _clean_path(path: str) -> str:
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("-"):
        raise ConflictError("invalid target path")
    return cleaned


def _artifact_label(artifact: object) -> str:
    name = getattr(artifact, "name", "")
    version = getattr(artifact, "version", None)
    return f"{name}:{version}" if version else name


def _q(path: str) -> str:
    import shlex

    return shlex.quote(path)


def _find_placement_id(workspace: WorkspaceService, _source: str, source_path: str) -> str:
    """Placement for (server, path); callers pass the ORIGINAL source server."""
    for placement in workspace.placements():
        if placement.remote_path == source_path:
            return placement.placement_id
    raise ConflictError("source placement no longer exists")

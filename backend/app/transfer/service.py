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
from typing import TYPE_CHECKING, Any

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
    from app.direct_auth.service import DirectAuthService
    from app.models.server import ServerRecord
    from app.models.transfer import DedicatedKeyOptions
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
        direct_auth: DirectAuthService | None = None,
    ) -> None:
        self._settings = settings
        self._ssh = ssh
        self._workspace = workspace
        self._direct_auth = direct_auth
        self._jobs = jobs or JobRegistry(
            history_limit=int(getattr(settings, "transfer_job_history", 100))
        )
        self._global_slot = asyncio.Semaphore(int(getattr(settings, "max_transfers_global", 2)))
        self._cancel_events: dict[str, asyncio.Event] = {}
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

    def clear_history(self) -> int:
        return self._jobs.clear_history()

    # ---- planning (creates no job) ------------------------------------------------

    def _resolve_excludes(self, artifact_id: str) -> list[str]:
        """Union of the referencing projects' exclude patterns.

        Any project's exclusion wins (the copy can only shrink, never grow);
        duplicates are dropped in first-seen order.
        """
        merged: list[str] = []
        for project in self._workspace.projects():
            if artifact_id not in project.artifact_ids:
                continue
            for pattern in project.transfer_excludes:
                if pattern and pattern not in merged:
                    merged.append(pattern)
        return merged

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
            excludes=self._resolve_excludes(request.artifact_id),
            probe_timeout_s=self._probe_timeout_s(),
            dedicated_key=self._dedicated_key_options(stub.source_server_id, stub.target_server_id),
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

        # Explicit per-artifact override wins; otherwise immutable unless code
        # (dataset/model targets must not be mutated by a later copy).
        immutable = (
            artifact.immutable if artifact.immutable is not None else artifact.kind != "code"
        )

        job = self._jobs.create(
            artifact_id=artifact.artifact_id,
            artifact_label=_artifact_label(artifact),
            source_server_id=placement.server_id,
            source_path=placement.remote_path,
            target_server_id=request.target_server_id,
            target_path=_clean_path(request.target_path),
            strategy_requested=request.strategy,
            excludes=self._resolve_excludes(request.artifact_id),
            immutable=immutable,
        )
        self._cancel_events[job.job_id] = asyncio.Event()
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
        # Wake the running worker NOW: the rsync path races this event against
        # the remote command and closes the session on cancel; the relay path
        # keeps observing job.state at its chunk checkpoints.
        event = self._cancel_events.get(job_id)
        if event is not None:
            event.set()

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
        finally:
            self._cancel_events.pop(job.job_id, None)

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
            excludes=list(job.excludes),
            probe_timeout_s=self._probe_timeout_s(),
            dedicated_key=self._dedicated_key_options(job.source_server_id, job.target_server_id),
        )
        if self._jobs.find(job.job_id) is None:
            return
        job.strategy_reason = plan.reason
        if plan.strategy_selected is None:
            self._jobs.fail(job, "strategy_unavailable", plan.reason[:300])
            return
        job.strategy_used = plan.strategy_selected
        if not await self._preflight_space(job, plan):
            return  # failed: not enough space and nothing to build on

        source_slot = self._ssh.transfer_slot_or_create(job.source_server_id)
        target_slot = self._ssh.transfer_slot_or_create(job.target_server_id)
        async with source_slot, target_slot:
            if self._jobs.find(job.job_id) is None:
                return
            try:
                if plan.strategy_selected is TransferStrategy.DIRECT_RSYNC:
                    await self._run_rsync(job, source_server, target_server, plan)
                else:
                    self._jobs.transition(job, TransferState.RUNNING)
                    source_session, target_session = await self._relay_sessions(
                        source_server, target_server
                    )
                    try:
                        await _relay(job, source_session, target_session, self._settings)
                    finally:
                        with contextlib.suppress(Exception):
                            await source_session.close()  # type: ignore[attr-defined]
            except CancelRequested:
                # cancel() already transitioned+archived; never double-archive.
                if job.state is not TransferState.CANCELLED:
                    self._jobs.transition(job, TransferState.CANCELLED)
                return

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
        plan: TransferPlan,
    ) -> None:
        from app.transfer.planner import build_rsync_command, parse_progress2, target_rsync_spec

        if job.state.value == TransferState.CANCELLED.value:
            raise CancelRequested(job.job_id)
        self._jobs.transition(job, TransferState.RUNNING)

        # Phase 4.2C: when planning selected the dedicated key, the pair's
        # metadata must STILL exist. If it vanished between plan and run we
        # fail the job loudly instead of silently falling back to a weaker
        # auth path (never a password, never the user's default key).
        dedicated: DedicatedKeyOptions | None = None
        if plan.direct_auth_method == "sgc_key":
            options = self._dedicated_key_options(job.source_server_id, job.target_server_id)
            if options is None:
                # Fail the job loudly: never fall back silently to a weaker
                # auth path (never a password, never the user's default key).
                raise ConflictError("direct transfer key is no longer configured")
            dedicated = options

        target_params = self._ssh.resolve_params_for(target_server)
        spec = target_rsync_spec(
            host=target_params.host,
            username=target_params.username,
            target_path=job.target_path,
        )
        command = build_rsync_command(
            source_path=job.source_path,
            target_spec=spec,
            target_port=target_params.port,
            excludes=list(job.excludes),
            dedicated_key=dedicated,
        )
        source_size_b = await self._source_size(job)
        if source_size_b is not None and source_size_b > 0:
            job.bytes_total = source_size_b

        async def _on_stdout(chunk: str) -> None:
            parse_progress2(chunk, job)
            if job.bytes_total is not None and job.bytes_done > job.bytes_total:
                job.bytes_done = job.bytes_total

        session = await self._ssh.transfer_command_session(source_server)
        run_task = asyncio.create_task(session.run(command, timeout_s=3600.0, on_stdout=_on_stdout))
        cancel_event = self._cancel_events.get(job.job_id)
        wait_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
        try:
            if wait_task is None:
                exit_code = await run_task
            else:
                race: set[asyncio.Task[Any]] = {run_task, wait_task}
                done, _pending = await asyncio.wait(race, return_when=asyncio.FIRST_COMPLETED)
                if run_task not in done and wait_task in done:
                    # User cancel won the race: close the channel so the remote
                    # rsync terminates now, reap the runner, then report cancel.
                    with contextlib.suppress(Exception):
                        await session.cancel()
                    await asyncio.gather(run_task, return_exceptions=True)
                    raise CancelRequested(job.job_id)
                if not wait_task.done():
                    wait_task.cancel()
                    await asyncio.gather(wait_task, return_exceptions=True)
                exit_code = run_task.result()  # re-raises transport failures
            if exit_code != 0:
                raise ConflictError(f"rsync exited with code {exit_code}")
        finally:
            await self._reap_rsync_tasks(run_task, wait_task)
            with contextlib.suppress(Exception):
                await session.close()

    @staticmethod
    async def _reap_rsync_tasks(
        run_task: asyncio.Task[int], wait_task: asyncio.Task[bool] | None
    ) -> None:
        tasks: list[asyncio.Task[Any]] = [run_task]
        if wait_task is not None:
            tasks.append(wait_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _preflight_space(self, job: TransferJob, plan: TransferPlan) -> bool:
        """Refuse hopeless transfers; warn and continue when the target exists.

        Only decides when BOTH probes succeeded and free < needed. A missing
        target with insufficient free space cannot succeed -> FAILED. An
        existing target may hold incremental data, so the estimate is not
        reliable -> warning only. Probe failures never block the job.
        """
        if not (
            plan.source_size_b is not None
            and plan.target_free_b is not None
            and plan.target_free_b < plan.source_size_b
        ):
            return True
        needed = plan.source_size_b
        free = plan.target_free_b
        try:
            probe = await self._executor(job.target_server_id).run(
                f"test -e {_q(job.target_path)}", timeout_s=10
            )
        except Exception:
            return True  # cannot probe the target: do not fail on a guess
        if probe.exit_code == 0:
            _add_warning(
                job,
                f"target may be insufficient: need {needed} B, only {free} B free,"
                " but the target already exists (incremental data may need less)",
            )
            return True
        self._jobs.fail(
            job,
            "insufficient_space",
            f"target {job.target_path} on {job.target_server_id} needs {needed} B"
            f" but only {free} B are free and the target does not exist yet",
        )
        return False

    def _probe_timeout_s(self) -> float:
        return float(getattr(self._settings, "transfer_preflight_timeout_s", 15.0))

    def _dedicated_key_options(self, source_id: str, target_id: str) -> DedicatedKeyOptions | None:
        """SOURCE-side dedicated-key options when the pair is configured (4.2C)."""

        if self._direct_auth is None:
            return None
        return self._direct_auth.dedicated_key_options(source_id, target_id)

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
        # Wake any worker parked in the rsync race so its session.cancel()
        # path runs before the raw task cancellation below.
        for event in self._cancel_events.values():
            event.set()
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
        immutable=job.immutable,
        excludes=list(job.excludes),
        progress_interval_s=float(getattr(settings, "transfer_progress_emit_interval_s", 0.5)),
    )


def _add_warning(job: TransferJob, message: str) -> None:
    """Bounded warning append: the model caps warnings at 20 entries."""
    if len(job.warnings) < 20:
        job.warnings.append(message[:300])


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

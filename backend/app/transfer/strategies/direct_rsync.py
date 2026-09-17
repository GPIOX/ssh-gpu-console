"""DIRECT_RSYNC strategy: run rsync ON the source server toward the target.

Only used after the planner's preflight passes (rsync both ends + BatchMode
source→target SSH). Command flags are fixed backend-side; paths are quoted by
the planner's builder; progress comes from --info=progress2 output lines.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.server import ServerRecord
from app.models.transfer import TransferJob
from app.transfer.planner import build_rsync_command, target_rsync_spec

# Single CancelRequested definition lives in local_relay; re-exported here so
# rsync-path consumers keep one import surface.
from app.transfer.strategies.local_relay import CancelRequested as CancelRequested

if TYPE_CHECKING:
    from app.ssh.manager import SshManager


async def rsync_transfer(
    *,
    job: TransferJob,
    ssh: SshManager,
    source_server: ServerRecord,
    target_server: ServerRecord,
    source_size_b: int | None,
) -> int:
    """Run rsync from the source server; returns its exit code.

    Mutates job: strategy_used, bytes/progress via --info=progress2 parsing.
    Transport errors propagate; user cancellation is decided by the service
    racing this command against its cancel event. The dedicated command
    session is closed on every exit path; a cancelled run leaves the
    remote-side --partial-dir partial in place.
    """
    target_params = ssh.resolve_params_for(target_server)
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
    )
    if source_size_b is not None and source_size_b > 0:
        job.bytes_total = source_size_b

    session = await ssh.transfer_command_session(source_server)

    async def _on_stdout(chunk: str) -> None:
        from app.transfer.planner import parse_progress2

        parse_progress2(chunk, job)
        if job.bytes_total is not None and job.bytes_done > job.bytes_total:
            job.bytes_done = job.bytes_total

    try:
        return await session.run(command, timeout_s=3600.0, on_stdout=_on_stdout)
    finally:
        await session.close()

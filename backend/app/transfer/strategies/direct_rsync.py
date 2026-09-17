"""DIRECT_RSYNC strategy: run rsync ON the source server toward the target.

Only used after the planner's preflight passes (rsync both ends + BatchMode
source→target SSH). Command flags are fixed backend-side; paths are quoted by
the planner's builder; progress comes from --info=progress2 output lines.
"""

from __future__ import annotations

import asyncio

from app.models.server import ServerRecord
from app.models.transfer import TransferJob
from app.ssh.manager import SshManager
from app.transfer.planner import build_rsync_command, target_rsync_spec


class CancelRequested(Exception):
    """Raised when the user cancels a running rsync."""


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
    Raises CancelRequested on user cancel; transport errors propagate.
    """
    target_params = ssh.resolve_params_for(target_server)
    spec = target_rsync_spec(
        host=target_params.host,
        port=target_params.port,
        username=target_params.username,
        target_path=job.target_path,
    )
    command = build_rsync_command(
        source_path=job.source_path, target_spec=spec, target_port=target_params.port
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
        exit_code = await session.run(command, timeout_s=3600.0, on_stdout=_on_stdout)
        return exit_code
    except asyncio.CancelledError:
        raise

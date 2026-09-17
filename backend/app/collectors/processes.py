"""Selected-server process snapshot collector.

The v3 spec emits ``pid user pcpu pmem rss stat comm args`` via the
equal-sign form, so the command name (``comm``) is separate from the full
command line (``args``) — the v2 bug where the binary name was duplicated
into the displayed command ("python python train.py") cannot recur here.
"""

from __future__ import annotations

import math

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import ProcessInfo
from app.ssh.commands import PROCESSES
from app.ssh.executor import RemoteCommandResult

# The processes table renders a capped subset, but the full listing is the
# correlation source for GPU compute processes (nvidia-smi compute-apps does
# not carry the user or commandline). 1200 rows covers busy login nodes.
_MAX_ROWS = 1200


def _percent(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 1)  # ps pcpu legitimately exceeds 100 on many threads


def parse_ps(stdout: str, max_rows: int = _MAX_ROWS) -> list[ProcessInfo]:
    """Parse the fixed ``ps`` field order while keeping ``args`` intact.

    ``split(None, 7)``: the eighth field keeps all remaining text, so
    arguments with spaces are preserved verbatim.
    """

    processes: list[ProcessInfo] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(None, 7)
        if len(fields) < 7:
            continue
        try:
            pid = int(fields[0])
        except (TypeError, ValueError):
            continue
        if pid < 1:
            continue
        user = fields[1][:64]
        cpu = _percent(fields[2])
        mem = _percent(fields[3])
        try:
            rss_b = max(0, int(fields[4])) * 1024
        except (TypeError, ValueError):
            rss_b = None
        state = fields[5][:16]
        comm = fields[6][:128]
        args = fields[7][:1000] if len(fields) > 7 else None
        processes.append(
            ProcessInfo(
                pid=pid,
                user=user,
                name=comm,
                cpu_percent=cpu,
                mem_percent=mem,
                rss_b=rss_b,
                state=state,
                command=args,
            )
        )
        if len(processes) >= max(1, max_rows):
            break
    return processes


class ProcessCollector:
    """Top processes by CPU at the PROCESS cadence."""

    name = "processes"
    spec = PROCESSES

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> list[ProcessInfo]:
        if result is None:
            result = await run_spec(executor, self.spec)
        if result.exit_code != 0:
            raise CollectorError(
                "command_failed",
                f"processes: ps failed (exit {result.exit_code}): {result.stderr.strip()[:300]}",
            )
        return parse_ps(result.stdout)

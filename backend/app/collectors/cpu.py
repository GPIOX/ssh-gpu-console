"""Low-cost CPU telemetry from one batched /proc command.

The utilization delta is kept per collector instance; the scheduler caches
one instance per server, so counter state survives between samples and is
dropped on identity change (worker restart).
"""

from __future__ import annotations

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import CpuInfo
from app.ssh.commands import CPU_MEMORY
from app.ssh.executor import RemoteCommandResult


def parse_proc_stat(
    stdout: str, previous: tuple[int, int] | None = None
) -> tuple[tuple[float | None, tuple[int, int]], tuple[float, float, float]]:
    """Parse the aggregate ``/proc/stat`` line and ``/proc/loadavg``.

    The first return value holds (utilization, counters-for-next-sample); a
    first sample or a counter reset yields ``None`` utilization rather than
    an invented value. Load averages degrade to zeroes when absent/malformed.
    """

    total: int | None = None
    idle: int | None = None
    loads: tuple[float, float, float] = (0.0, 0.0, 0.0)
    for line in stdout.splitlines():
        parts = line.split()
        if line.startswith("cpu ") and total is None:
            try:
                counters = [int(item) for item in parts[1:9]]
            except ValueError:
                continue
            if len(counters) < 4:
                continue
            # idle + iowait are both non-work time on Linux.
            total = sum(counters)
            idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
        elif len(parts) == 5 and "/" in parts[3]:
            # /proc/loadavg: three floats, then "running/threads" and last pid.
            try:
                first, second, third = (float(item) for item in parts[:3])
            except ValueError:
                continue
            loads = (first, second, third)
    if total is None or idle is None:
        raise CollectorError("parse_error", "cpu: aggregate /proc/stat line missing")
    if previous is None or total <= previous[0] or idle < previous[1]:
        return (None, (total, idle)), loads
    total_delta = total - previous[0]
    idle_delta = max(0, idle - previous[1])
    utilization = round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)
    return (utilization, (total, idle)), loads


def parse_cpu(stdout: str, previous: tuple[int, int] | None = None) -> CpuInfo:
    (utilization, _counters), loads = parse_proc_stat(stdout, previous)
    return CpuInfo(percent=utilization, load_1=loads[0], load_5=loads[1], load_15=loads[2])


class CpuCollector:
    """CPU utilization + load averages; shares the CPU_MEMORY command."""

    name = "cpu"
    spec = CPU_MEMORY

    def __init__(self) -> None:
        self._previous: tuple[int, int] | None = None

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> CpuInfo:
        if result is None:
            result = await run_spec(executor, self.spec)
        if result.exit_code != 0:
            raise CollectorError(
                "command_failed",
                f"cpu: /proc read failed (exit {result.exit_code}): {result.stderr.strip()[:300]}",
            )
        (utilization, counters), loads = parse_proc_stat(result.stdout, self._previous)
        self._previous = counters
        return CpuInfo(percent=utilization, load_1=loads[0], load_5=loads[1], load_15=loads[2])

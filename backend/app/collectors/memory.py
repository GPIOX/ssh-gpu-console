"""Memory telemetry from the same batched command used by CPU collection."""

from __future__ import annotations

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import MemoryInfo
from app.ssh.commands import CPU_MEMORY
from app.ssh.executor import RemoteCommandResult

_UNIT_MULTIPLIERS = {
    "": 1,
    "b": 1,
    "kb": 1024,
    "kib": 1024,
    "mb": 1024 * 1024,
    "mib": 1024 * 1024,
    "gb": 1024 * 1024 * 1024,
    "gib": 1024 * 1024 * 1024,
}


def parse_meminfo(stdout: str) -> MemoryInfo:
    """Parse ``/proc/meminfo``; values carry the normal kB unit and are
    normalized to bytes. ``MemAvailable`` falls back to ``MemFree`` on old
    kernels. Missing keys stay ``None`` — nothing is fabricated.
    """

    values: dict[str, int] = {}
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        unit = fields[1].lower() if len(fields) > 1 else ""
        multiplier = _UNIT_MULTIPLIERS.get(unit)
        if multiplier is None:
            continue
        values[key.strip()] = max(0, value * multiplier)
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    if total is not None and available is not None:
        available = min(available, total)
    used: int | None = None
    if total is not None and available is not None:
        used = max(0, total - available)
    percent: float | None = None
    if used is not None and total:
        percent = round(used / total * 100.0, 1)
    return MemoryInfo(total_b=total, used_b=used, available_b=available, percent=percent)


class MemoryCollector:
    """Memory totals/usage; shares the CPU_MEMORY command with the CPU collector."""

    name = "memory"
    spec = CPU_MEMORY

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> MemoryInfo:
        if result is None:
            result = await run_spec(executor, self.spec)
        if result.exit_code != 0:
            raise CollectorError(
                "command_failed",
                f"memory: /proc read failed (exit {result.exit_code}): "
                f"{result.stderr.strip()[:300]}",
            )
        return parse_meminfo(result.stdout)

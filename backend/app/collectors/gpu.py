"""Batched agentless NVIDIA collectors.

The GPU query is one fixed command per sample. Drivers that reject an
extended field fall back to ``GPU_BASIC`` once, then stick to it. A missing
binary, broken driver or GPU-less host is a capability fact, not a crash:
the collector raises ``CollectorError("unavailable", ...)`` so the section
degrades while unrelated sections keep flowing. Transport failures arrive
as ``ExecutorError`` and are re-raised as ``CollectorError`` with the same
code — never mapped to a remote exit code.
"""

from __future__ import annotations

import csv
import math

from app.collectors.base import SECTION_GPU_PROCESSES, CollectorError, ExecutorLike
from app.models.telemetry import FleetProcsSample, GpuAvailability, GpuInfo, GpuProcessInfo
from app.ssh.commands import FLEET_GPU_PROCS, GPU, GPU_BASIC, GPU_PROCESSES, CommandSpec
from app.ssh.executor import ExecutorError, RemoteCommandResult

# stderr markers that mean "this host has no usable NVIDIA stack".
_UNAVAILABLE_MARKERS = (
    "command not found",
    "no such file",
    "no devices were found",
    "couldn't communicate with the nvidia driver",
    "failed to initialize nvml",
    "driver/library version mismatch",
)

# stderr markers that mean "the driver rejected an extended query field".
_INVALID_FIELD_MARKERS = ("invalid query", "unknown field", "not supported")

_MIB = 1024 * 1024


def _number(value: str | None) -> float | None:
    raw = (value or "").strip()
    if not raw or raw.upper() in {"N/A", "NA", "[N/A]", "NOT SUPPORTED"}:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: str | None) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _bounded(value: float | None, low: float, high: float) -> float | None:
    return value if value is not None and low <= value <= high else None


def _megabytes_to_bytes(value: str | None) -> int | None:
    number = _number(value)
    return int(number * _MIB) if number is not None and number >= 0 else None


def _cells(stdout: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in csv.reader(stdout.splitlines(), skipinitialspace=True):
        cells = [cell.strip() for cell in row]
        if any(cells):
            rows.append(cells)
    return rows


def derive_availability(utilization: float | None, vram_percent: float | None) -> GpuAvailability:
    """Busy/free classification shared by collectors and state."""
    if (utilization is not None and utilization > 80.0) or (
        vram_percent is not None and vram_percent >= 95.0
    ):
        return GpuAvailability.SATURATED
    if (utilization is not None and utilization > 5.0) or (
        vram_percent is not None and vram_percent > 5.0
    ):
        return GpuAvailability.ACTIVE
    return GpuAvailability.FREE


def parse_gpu_query(stdout: str, *, basic: bool = False) -> list[GpuInfo]:
    """Parse one multi-field ``nvidia-smi`` CSV query.

    A malformed row is discarded; an unsupported individual cell becomes
    ``None`` so mixed drivers and MIG-like partial output stay useful.
    Full layout: index,uuid,name,driver_version,util,memory.used,memory.total,
    temperature,power.draw,power.limit,fan.speed. The basic layout is
    index,name,util,memory.used,memory.total,temperature.
    """

    gpus: list[GpuInfo] = []
    for cells in _cells(stdout):
        if len(cells) < 5:
            continue
        index = _integer(cells[0])
        if index is None:
            continue
        if basic:
            uuid: str | None = None
            name = cells[1] or None
            utilization = _bounded(_number(cells[2]), 0.0, 100.0)
            memory_used = _megabytes_to_bytes(cells[3]) if len(cells) > 3 else None
            memory_total = _megabytes_to_bytes(cells[4]) if len(cells) > 4 else None
            temperature = _bounded(_number(cells[5]) if len(cells) > 5 else None, -50.0, 150.0)
            power = power_limit = fan = None
        else:
            uuid = cells[1] or None
            name = cells[2] or None
            utilization = _bounded(_number(cells[4]), 0.0, 100.0)
            memory_used = _megabytes_to_bytes(cells[5]) if len(cells) > 5 else None
            memory_total = _megabytes_to_bytes(cells[6]) if len(cells) > 6 else None
            temperature = _bounded(_number(cells[7]) if len(cells) > 7 else None, -50.0, 150.0)
            power = _bounded(_number(cells[8]) if len(cells) > 8 else None, 0.0, 10000.0)
            power_limit = _bounded(_number(cells[9]) if len(cells) > 9 else None, 0.0, 10000.0)
            fan = _bounded(_number(cells[10]) if len(cells) > 10 else None, 0.0, 100.0)
        vram_percent: float | None = None
        if memory_used is not None and memory_total:
            vram_percent = round(memory_used / memory_total * 100.0, 1)
        gpus.append(
            GpuInfo(
                index=index,
                uuid=uuid,
                name=name,
                utilization_percent=utilization,
                vram_used_b=memory_used,
                vram_total_b=memory_total,
                vram_percent=vram_percent,
                temperature_c=temperature,
                power_watts=power,
                power_limit_watts=power_limit,
                fan_percent=fan,
                availability=derive_availability(utilization, vram_percent),
            )
        )
    return gpus


def parse_gpu_processes(stdout: str) -> list[GpuProcessInfo]:
    """Parse compute-app rows; processes exiting between queries are normal.

    Handles the four-field spec layout plus three-field rows from drivers
    that do not report the process name.
    """

    processes: list[GpuProcessInfo] = []
    for cells in _cells(stdout):
        if len(cells) < 3:
            continue
        try:
            pid = int(cells[0])
        except (TypeError, ValueError):
            continue
        if pid < 1:
            continue
        if len(cells) == 3:
            name: str | None = None
            memory = cells[1]
            gpu_uuid = cells[2]
        elif len(cells) == 4:
            name = cells[1] or None
            memory = cells[2]
            gpu_uuid = cells[3]
        else:
            name = ",".join(cells[1:-2]) or None
            memory = cells[-2]
            gpu_uuid = cells[-1]
        processes.append(
            GpuProcessInfo(
                pid=pid,
                gpu_uuid=gpu_uuid or None,
                process_name=name,
                used_memory_b=_megabytes_to_bytes(memory),
            )
        )
    return processes


def _is_unavailable(stderr: str, exit_code: int) -> bool:
    text = stderr.lower()
    return exit_code == 127 or any(marker in text for marker in _UNAVAILABLE_MARKERS)


def _has_invalid_field(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _INVALID_FIELD_MARKERS)


_PS_MARKER = "---PS---"


def parse_fleet_gpu_procs(stdout: str) -> FleetProcsSample:
    """Split the combined fleet probe: compute-app rows + pid->user lines."""

    marker = stdout.find(_PS_MARKER)
    if marker < 0:
        return FleetProcsSample(
            gpu_processes=parse_gpu_processes(stdout),
            pid_users={},
        )
    procs = parse_gpu_processes(stdout[:marker])
    pid_users: dict[str, str] = {}
    for line in stdout[marker + len(_PS_MARKER) :].splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid_users[parts[0]] = parts[1]
    return FleetProcsSample(gpu_processes=procs, pid_users=pid_users)


async def run_spec(executor: ExecutorLike, spec: CommandSpec) -> RemoteCommandResult:
    """Run one fixed spec, converting transport failure to CollectorError.

    The code string is preserved so the scheduler can map e.g. ``timeout``
    to the matching server status without re-classifying.
    """

    try:
        return await executor.run(spec.command, timeout_s=spec.timeout_s)
    except ExecutorError as exc:
        raise CollectorError(exc.code, exc.detail) from exc
    except Exception as exc:  # a raw transport leak must never kill a sample
        code = str(getattr(exc, "code", "unknown"))
        detail = str(getattr(exc, "detail", exc))
        raise CollectorError(code, detail) from exc


class GpuCollector:
    """GPU inventory + utilization at the FAST cadence."""

    name = "gpu"

    def __init__(self) -> None:
        self._basic_only = False

    @property
    def spec(self) -> CommandSpec:
        return GPU_BASIC if self._basic_only else GPU

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> list[GpuInfo]:
        if self._basic_only:
            return self._collect_basic(await run_spec(executor, GPU_BASIC))
        outcome = result if result is not None else await run_spec(executor, GPU)
        if outcome.exit_code == 0:
            return parse_gpu_query(outcome.stdout)
        stderr = outcome.stderr
        if _is_unavailable(stderr, outcome.exit_code):
            raise CollectorError(
                "unavailable", stderr.strip()[:300] or "nvidia-smi or NVIDIA driver unavailable"
            )
        if _has_invalid_field(stderr):
            self._basic_only = True
            return self._collect_basic(await run_spec(executor, GPU_BASIC))
        raise CollectorError(
            "command_failed",
            f"gpu query failed (exit {outcome.exit_code}): {stderr.strip()[:300]}",
        )

    @staticmethod
    def _collect_basic(outcome: RemoteCommandResult) -> list[GpuInfo]:
        if outcome.exit_code != 0:
            if _is_unavailable(outcome.stderr, outcome.exit_code):
                raise CollectorError(
                    "unavailable", outcome.stderr.strip()[:300] or "nvidia-smi unavailable"
                )
            raise CollectorError(
                "command_failed",
                f"gpu_basic query failed (exit {outcome.exit_code}): "
                f"{outcome.stderr.strip()[:300]}",
            )
        return parse_gpu_query(outcome.stdout, basic=True)


class GpuProcessCollector:
    """Per-GPU compute processes at the MEDIUM cadence."""

    name = "gpu_processes"
    spec = GPU_PROCESSES

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> list[GpuProcessInfo]:
        outcome = result if result is not None else await run_spec(executor, GPU_PROCESSES)
        if outcome.exit_code == 0:
            return parse_gpu_processes(outcome.stdout)
        stderr = outcome.stderr
        if _is_unavailable(stderr, outcome.exit_code):
            raise CollectorError("unavailable", stderr.strip()[:300] or "nvidia-smi unavailable")
        raise CollectorError(
            "command_failed",
            f"GPU process query failed (exit {outcome.exit_code}): {stderr.strip()[:300]}",
        )


class FleetGpuProcCollector:
    """Fleet-tier combined probe: compute apps + owning users in one command."""

    name = SECTION_GPU_PROCESSES
    spec = FLEET_GPU_PROCS

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> FleetProcsSample:
        outcome = result if result is not None else await run_spec(executor, FLEET_GPU_PROCS)
        if outcome.exit_code != 0:
            if _is_unavailable(outcome.stderr, outcome.exit_code):
                raise CollectorError(
                    "unavailable", outcome.stderr.strip()[:300] or "nvidia-smi unavailable"
                )
            raise CollectorError(
                "command_failed",
                f"fleet gpu-procs probe failed (exit {outcome.exit_code}): "
                f"{outcome.stderr.strip()[:300]}",
            )
        return parse_fleet_gpu_procs(outcome.stdout)

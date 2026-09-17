"""Shared latest telemetry and bounded in-memory history.

One process-local state object is shared by REST, the scheduler and the
realtime hub. Sections update independently; every mutation is reflected in
a global revision (any change) and a fleet revision (fleet-visible change
only) so the hub can skip resending unchanged frames. Nothing here ever
touches disk.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.gpu import derive_availability
from app.models.server import ServerRecord
from app.models.telemetry import (
    CpuInfo,
    FleetEntry,
    FleetGpu,
    FleetSummary,
    GpuInfo,
    GpuProcessInfo,
    HistoryPoint,
    MemoryInfo,
    NetworkInterfaceInfo,
    ProcessInfo,
    ServerSnapshot,
    ServerStatus,
    StorageMount,
    SystemInfo,
)
from app.telemetry.history import GpuSeries

_FLEET_STALE_MARGIN = 5.0
_GPU_BUSY_AVAILABILITY = frozenset(("active", "saturated"))  # compared via enum values below
_DISK_WARN_PERCENT = 90.0
_MAX_GPUS = 64
_MAX_GPU_PROCESSES = 4096
_MAX_PROCESSES = 250
_MAX_PID_USERS = 2048
_MAX_GPU_USERS = 8
# pid -> (user, command) correlation index size (full ps sample)
_MAX_PROC_META = 2048
_MAX_MOUNTS = 512
_MAX_INTERFACES = 512


def utcnow() -> datetime:
    return datetime.now(UTC)


def _endpoint(record: ServerRecord) -> str:
    if record.username and record.port:
        return f"{record.username}@{record.ssh_host}:{record.port}"
    if record.username:
        return f"{record.username}@{record.ssh_host}"
    if record.port:
        return f"{record.ssh_host}:{record.port}"
    return record.ssh_host


@dataclass
class _ServerState:
    """Mutable per-server telemetry; sections update independently."""

    server_id: str
    status: ServerStatus = ServerStatus.UNKNOWN
    status_detail: str = ""
    status_updated_at: datetime | None = None
    cpu: CpuInfo | None = None
    memory: MemoryInfo | None = None
    gpus: list[GpuInfo] = field(default_factory=list)
    gpu_processes: list[GpuProcessInfo] = field(default_factory=list)
    proc_meta: dict[int, tuple[str | None, str | None]] = field(default_factory=dict)
    pid_users: dict[int, str] = field(default_factory=dict)  # fleet-tier probe
    processes: list[ProcessInfo] = field(default_factory=list)
    storage: list[StorageMount] = field(default_factory=list)
    network: list[NetworkInterfaceInfo] = field(default_factory=list)
    system: SystemInfo | None = None
    errors: dict[str, str] = field(default_factory=dict)
    updated_at: dict[str, datetime] = field(default_factory=dict)
    version: int = 0

    def latest_update(self) -> datetime | None:
        stamps = [self.status_updated_at, *self.updated_at.values()]
        present = [stamp for stamp in stamps if stamp is not None]
        return max(present) if present else None


def _gpu_users(state: _ServerState, gpu: GpuInfo) -> list[str]:
    """Distinct owning users of one GPU from correlated compute processes."""
    users: list[str] = []
    for process in state.gpu_processes:
        belongs = process.gpu_index == gpu.index or (
            process.gpu_uuid is not None and process.gpu_uuid == gpu.uuid
        )
        if process.user and process.user not in users and belongs:
            users.append(process.user)
            if len(users) >= _MAX_GPU_USERS:
                break
    return users


class TelemetryState:
    """Latest values + bounded history + per-section errors per server."""

    def __init__(self, settings: Any) -> None:
        self._history_points = self._bounded_points(getattr(settings, "history_points", 300))
        self._fleet_interval_active = float(getattr(settings, "fleet_interval_active", 15.0))
        self._fleet_interval_idle = float(getattr(settings, "fleet_interval_idle", 45.0))
        self._servers: dict[str, _ServerState] = {}
        self._gpu_history: dict[str, dict[int, GpuSeries]] = {}
        self._net_prev: dict[str, dict[str, tuple[float, int, int]]] = {}
        self._revision = 0
        self._fleet_revision = 0
        self._listener: Callable[[], None] | None = None

    # ---- change notification ------------------------------------------------

    def set_listener(self, listener: Callable[[], None] | None) -> None:
        """Register a zero-argument callback fired after every mutation."""

        self._listener = listener

    @staticmethod
    def _bounded_points(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = 300
        return max(1, min(parsed, 10_000))

    def _bump(self, server_id: str, *, fleet: bool = True) -> None:
        state = self._servers.get(server_id)
        if state is not None:
            state.version += 1
        self._revision += 1
        if fleet:
            self._fleet_revision += 1
        if self._listener is not None:
            self._listener()

    def revision(self) -> int:
        return self._revision

    def fleet_revision(self) -> int:
        return self._fleet_revision

    # ---- container access ---------------------------------------------------

    def _get(self, server_id: str) -> _ServerState:
        state = self._servers.get(server_id)
        if state is None:
            state = _ServerState(server_id=server_id)
            self._servers[server_id] = state
        return state

    def exists(self, server_id: str) -> bool:
        return server_id in self._servers

    def server_ids(self) -> list[str]:
        return list(self._servers)

    def status_of(self, server_id: str) -> ServerStatus:
        state = self._servers.get(server_id)
        return state.status if state is not None else ServerStatus.UNKNOWN

    def errors_of(self, server_id: str) -> dict[str, str]:
        state = self._servers.get(server_id)
        return dict(state.errors) if state is not None else {}

    def version_of(self, server_id: str) -> int:
        state = self._servers.get(server_id)
        return state.version if state is not None else 0

    # ---- section updates ----------------------------------------------------

    def set_status(self, server_id: str, status: ServerStatus, detail: str = "") -> None:
        state = self._get(server_id)
        if state.status == status and state.status_detail == detail:
            return
        state.status = status
        state.status_detail = detail[:300]
        state.status_updated_at = utcnow()
        self._bump(server_id)

    def update_cpu(self, server_id: str, cpu: CpuInfo) -> None:
        state = self._get(server_id)
        state.cpu = cpu
        state.updated_at["cpu"] = utcnow()
        self._bump(server_id)

    def update_memory(self, server_id: str, memory: MemoryInfo) -> None:
        state = self._get(server_id)
        state.memory = memory
        state.updated_at["memory"] = utcnow()
        self._bump(server_id)

    def update_system(self, server_id: str, system: SystemInfo) -> None:
        state = self._get(server_id)
        state.system = system
        state.updated_at["system"] = utcnow()
        # cores_logical is a CPU-section field sourced from the static probe.
        if state.cpu is not None and system.cores_logical is not None:
            state.cpu.cores_logical = system.cores_logical
        self._bump(server_id)

    def update_gpus(self, server_id: str, gpus: Sequence[GpuInfo]) -> None:
        state = self._get(server_id)
        bounded = list(gpus)[:_MAX_GPUS]
        for gpu in bounded:
            if gpu.vram_percent is None and gpu.vram_used_b is not None and gpu.vram_total_b:
                gpu.vram_percent = round(gpu.vram_used_b / gpu.vram_total_b * 100.0, 1)
            # One source of truth for busy/free regardless of who built the model.
            gpu.availability = derive_availability(gpu.utilization_percent, gpu.vram_percent)
        state.gpus = bounded
        state.updated_at["gpu"] = utcnow()
        self._record_gpu_history(server_id, state.gpus)
        self._correlate(state)
        self._bump(server_id)

    def update_pid_users(self, server_id: str, pid_users: Mapping[str, str]) -> None:
        """Fleet-tier pid->user probe result (correlation-only, no disk)."""
        state = self._get(server_id)
        state.pid_users = {
            int(pid): user
            for pid, user in list(pid_users.items())[:_MAX_PID_USERS]
            if pid.isdigit()
        }
        self._correlate(state)

    def update_gpu_processes(self, server_id: str, processes: Sequence[GpuProcessInfo]) -> None:
        state = self._get(server_id)
        state.gpu_processes = list(processes)[:_MAX_GPU_PROCESSES]
        state.updated_at["gpu_processes"] = utcnow()
        self._correlate(state)
        self._bump(server_id, fleet=False)

    def update_processes(self, server_id: str, processes: Sequence[ProcessInfo]) -> None:
        state = self._get(server_id)
        state.processes = list(processes)[:_MAX_PROCESSES]
        # Correlation index over the FULL sample: a compute process may sit
        # below the rendered top-N but must still resolve user/command.
        state.proc_meta = {
            process.pid: (process.user, process.command)
            for process in list(processes)[:_MAX_PROC_META]
        }
        state.updated_at["processes"] = utcnow()
        self._correlate(state)
        self._bump(server_id, fleet=False)

    def update_storage(self, server_id: str, mounts: Sequence[StorageMount]) -> None:
        state = self._get(server_id)
        state.storage = list(mounts)[:_MAX_MOUNTS]
        state.updated_at["storage"] = utcnow()
        self._bump(server_id, fleet=False)

    def update_network(self, server_id: str, interfaces: Sequence[NetworkInterfaceInfo]) -> None:
        state = self._get(server_id)
        now = time.monotonic()
        previous = self._net_prev.setdefault(server_id, {})
        bounded = list(interfaces)[:_MAX_INTERFACES]
        for info in bounded:
            old = previous.get(info.name)
            rx_total = info.rx_total_b
            tx_total = info.tx_total_b
            if old is None or rx_total is None or tx_total is None:
                # First sample, unknown counters, or counter reset: no rate.
                info.rx_bps = None
                info.tx_bps = None
            else:
                elapsed = now - old[0]
                info.rx_bps = (
                    (rx_total - old[1]) / elapsed if elapsed > 0 and rx_total >= old[1] else None
                )
                info.tx_bps = (
                    (tx_total - old[2]) / elapsed if elapsed > 0 and tx_total >= old[2] else None
                )
            if rx_total is None or tx_total is None:
                previous.pop(info.name, None)  # unknown baseline: restart next sample
            else:
                previous[info.name] = (now, rx_total, tx_total)
        state.network = bounded
        state.updated_at["network"] = utcnow()
        self._bump(server_id, fleet=False)

    def set_error(self, server_id: str, section: str, code: str) -> None:
        """Record a bounded collector diagnostic; an empty code clears it."""

        allowed = {
            "system",
            "cpu",
            "memory",
            "gpu",
            "gpu_processes",
            "storage",
            "network",
            "processes",
        }
        if section not in allowed:
            return
        state = self._get(server_id)
        text = str(code).strip()[:300]
        if not text:
            state.errors.pop(section, None)
            self._bump(server_id)
        elif state.errors.get(section) != text:
            state.errors[section] = text
            self._bump(server_id)

    def clear_error(self, server_id: str, section: str) -> None:
        self.set_error(server_id, section, "")

    # ---- correlation --------------------------------------------------------

    def _correlate(self, state: _ServerState) -> None:
        """Join GPU processes with general processes (by pid) and GPU indexes
        (by uuid). Both directions stay consistent whichever section updated."""

        by_pid = {process.pid: process for process in state.processes}
        meta = state.proc_meta
        fleet_users = state.pid_users
        by_uuid = {gpu.uuid: gpu.index for gpu in state.gpus if gpu.uuid}
        per_gpu_count: dict[int, int] = {}
        per_pid: dict[int, list[int]] = {}
        per_pid_vram: dict[int, int] = {}
        for process in state.gpu_processes:
            if process.gpu_uuid:
                process.gpu_index = by_uuid.get(process.gpu_uuid)
            general = by_pid.get(process.pid)
            if general is not None:
                process.user = general.user
                process.command = general.command
                process.process_name = process.process_name or general.name
            elif process.pid in meta:
                meta_user, meta_command = meta[process.pid]
                if process.user is None:
                    process.user = meta_user
                if process.command is None:
                    process.command = meta_command
            elif process.user is None and process.pid in fleet_users:
                process.user = fleet_users[process.pid]
            if process.gpu_index is not None:
                per_gpu_count[process.gpu_index] = per_gpu_count.get(process.gpu_index, 0) + 1
                indexes = per_pid.setdefault(process.pid, [])
                if process.gpu_index not in indexes:
                    indexes.append(process.gpu_index)
                if process.used_memory_b is not None:
                    per_pid_vram[process.pid] = per_pid_vram.get(process.pid, 0) + (
                        process.used_memory_b
                    )
        state.gpu_processes.sort(
            key=lambda item: (item.gpu_index is None, item.gpu_index or 0, item.pid)
        )
        for gpu in state.gpus:
            gpu.process_count = per_gpu_count.get(gpu.index, 0)
        for proc in state.processes:
            proc.gpu_indexes = sorted(per_pid.get(proc.pid, []))
            proc.gpu_vram_b = per_pid_vram.get(proc.pid)

    def _record_gpu_history(self, server_id: str, gpus: list[GpuInfo]) -> None:
        now = utcnow()
        by_index = self._gpu_history.setdefault(server_id, {})
        for gpu in gpus:
            series = by_index.get(gpu.index)
            if series is None:
                series = GpuSeries(maxlen=self._history_points)
                by_index[gpu.index] = series
            if gpu.utilization_percent is not None:
                series.utilization.append(now, gpu.utilization_percent)
            if gpu.vram_used_b is not None:
                series.vram.append(now, gpu.vram_used_b)
            if gpu.temperature_c is not None:
                series.temperature.append(now, gpu.temperature_c)
            if gpu.power_watts is not None:
                series.power.append(now, gpu.power_watts)

    def gpu_history(self, server_id: str, gpu_index: int, metric: str) -> list[HistoryPoint]:
        by_index = self._gpu_history.get(server_id)
        if by_index is None:
            return []
        series = by_index.get(gpu_index)
        if series is None:
            return []
        return series.series(metric).as_list()

    # ---- snapshots ----------------------------------------------------------

    def _fleet_stale_after(self) -> float:
        """Freshness window covering both fleet sampling modes."""

        return max(
            self._fleet_interval_active * 3.0,
            self._fleet_interval_idle * 2.0 + _FLEET_STALE_MARGIN,
        )

    def snapshot(self, server_id: str) -> ServerSnapshot | None:
        """Deep copy so API serialization cannot mutate shared state."""

        state = self._servers.get(server_id)
        if state is None:
            return None
        latest = state.latest_update()
        stale = latest is None or (utcnow() - latest) > timedelta(seconds=self._fleet_stale_after())
        snapshot = ServerSnapshot(
            server_id=server_id,
            status=state.status,
            generated_at=utcnow(),
            cpu=state.cpu,
            memory=state.memory,
            gpus=list(state.gpus),
            gpu_processes=list(state.gpu_processes),
            processes=list(state.processes),
            storage=list(state.storage),
            network=list(state.network),
            system=state.system,
            errors=dict(state.errors),
            stale=stale,
        )
        return snapshot.model_copy(deep=True)

    def fleet_summary(self, records: Sequence[ServerRecord]) -> FleetSummary:
        entries: list[FleetEntry] = []
        for record in records:
            state = self._servers.get(record.server_id)
            gpus = state.gpus if state is not None else []
            gpu_users: list[list[str]] = (
                [_gpu_users(state, gpu) for gpu in gpus]
                if state is not None
                else [[] for _ in gpus]
            )
            gpu_busy = sum(1 for gpu in gpus if gpu.availability.value in _GPU_BUSY_AVAILABILITY)
            gpu_free = sum(1 for gpu in gpus if gpu.availability.value == "free")
            vram_used = sum(gpu.vram_used_b or 0 for gpu in gpus)
            vram_total = sum(gpu.vram_total_b or 0 for gpu in gpus)
            disk_warning = any(
                mount.percent is not None and mount.percent >= _DISK_WARN_PERCENT
                for mount in (state.storage if state is not None else [])
            )
            entries.append(
                FleetEntry(
                    server_id=record.server_id,
                    display_name=record.display_name,
                    ssh_endpoint=_endpoint(record),
                    status=state.status if state is not None else ServerStatus.UNKNOWN,
                    enabled=record.enabled,
                    os_pretty=(
                        state.system.os_pretty if state is not None and state.system else None
                    ),
                    gpu_model=next((gpu.name for gpu in gpus if gpu.name), None),
                    gpu_count=len(gpus),
                    gpu_busy=gpu_busy,
                    gpu_free=gpu_free,
                    cpu_percent=state.cpu.percent if state is not None and state.cpu else None,
                    memory_percent=(
                        state.memory.percent if state is not None and state.memory else None
                    ),
                    vram_used_b=vram_used or None,
                    vram_total_b=vram_total or None,
                    disk_warning=disk_warning,
                    gpus=[
                        FleetGpu(
                            index=gpu.index,
                            utilization_percent=gpu.utilization_percent,
                            vram_used_b=gpu.vram_used_b,
                            vram_total_b=gpu.vram_total_b,
                            temperature_c=gpu.temperature_c,
                            power_watts=gpu.power_watts,
                            power_limit_watts=gpu.power_limit_watts,
                            users=gpu_users[gpu_i],
                            availability=gpu.availability,
                        )
                        for gpu_i, gpu in enumerate(gpus[:64])
                    ],
                    updated_at=state.latest_update() if state is not None else None,
                )
            )
        return FleetSummary(generated_at=utcnow(), servers=entries)

    # ---- lifecycle ----------------------------------------------------------

    def clear(self, server_id: str) -> None:
        """Drop all data for one server (used on identity change)."""

        self._servers.pop(server_id, None)
        self._gpu_history.pop(server_id, None)
        self._net_prev.pop(server_id, None)
        self._revision += 1
        self._fleet_revision += 1
        if self._listener is not None:
            self._listener()

    def prune(self, keep: Iterable[str]) -> None:
        allowed = set(keep)
        for server_id in list(self._servers):
            if server_id not in allowed:
                self.clear(server_id)

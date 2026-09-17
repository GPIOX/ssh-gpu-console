"""Shared telemetry wire models.

Semantics (binding):
- `None` means "not reported by the remote host" — render N/A, never fabricate.
- bytes fields are ints and suffixed `_b`; percentages are floats 0..100.
- timestamps are wall-clock datetimes (UTC) for display; rates are computed in state.
- Parsers must never raise on unsupported/absent fields; they return None instead.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ServerStatus(StrEnum):
    UNKNOWN = "unknown"
    CONNECTING = "connecting"
    ONLINE = "online"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"
    TIMEOUT = "timeout"
    AUTHENTICATION_FAILED = "authentication_failed"
    HOST_KEY_ERROR = "host_key_error"
    DEGRADED = "degraded"


class GpuAvailability(StrEnum):
    FREE = "free"
    ACTIVE = "active"
    SATURATED = "saturated"
    UNAVAILABLE = "unavailable"  # nvidia-smi missing / driver broken / no GPU


class CpuInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    percent: float | None = None
    load_1: float | None = None
    load_5: float | None = None
    load_15: float | None = None
    cores_logical: int | None = None


class MemoryInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_b: int | None = None
    used_b: int | None = None
    available_b: int | None = None
    percent: float | None = None


class GpuInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    uuid: str | None = None
    name: str | None = None
    utilization_percent: float | None = None
    vram_used_b: int | None = None
    vram_total_b: int | None = None
    vram_percent: float | None = None
    temperature_c: float | None = None
    power_watts: float | None = None
    power_limit_watts: float | None = None
    fan_percent: float | None = None
    availability: GpuAvailability = GpuAvailability.UNAVAILABLE
    process_count: int = 0


class GpuProcessInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int
    gpu_uuid: str | None = None
    gpu_index: int | None = None
    used_memory_b: int | None = None
    process_name: str | None = None
    user: str | None = None
    command: str | None = None


class ProcessInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pid: int
    user: str | None = None
    name: str | None = None
    cpu_percent: float | None = None
    mem_percent: float | None = None
    rss_b: int | None = None
    state: str | None = None
    command: str | None = None
    gpu_indexes: list[int] = Field(default_factory=list)
    gpu_vram_b: int | None = None


class StorageMount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str
    fstype: str | None = None
    mount: str
    total_b: int | None = None
    used_b: int | None = None
    free_b: int | None = None
    percent: float | None = None


class NetworkInterfaceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ip: str | None = None
    rx_bps: float | None = None
    tx_bps: float | None = None
    rx_total_b: int | None = None
    tx_total_b: int | None = None


class SystemInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    os_pretty: str | None = None
    kernel: str | None = None
    uptime_s: float | None = None
    cpu_model: str | None = None
    cores_physical: int | None = None
    cores_logical: int | None = None
    driver_version: str | None = None


class FleetProcsSample(BaseModel):
    """Fleet-tier GPU compute processes plus the pid->user probe result."""

    model_config = ConfigDict(extra="forbid")

    gpu_processes: list[GpuProcessInfo] = Field(default_factory=list)
    pid_users: dict[str, str] = Field(default_factory=dict)


class ServerSnapshot(BaseModel):
    """Full detail snapshot for one server. Sections may be absent/stale independently."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    status: ServerStatus = ServerStatus.UNKNOWN
    generated_at: datetime | None = None
    cpu: CpuInfo | None = None
    memory: MemoryInfo | None = None
    gpus: list[GpuInfo] = Field(default_factory=list)
    gpu_processes: list[GpuProcessInfo] = Field(default_factory=list)
    processes: list[ProcessInfo] = Field(default_factory=list)
    storage: list[StorageMount] = Field(default_factory=list)
    network: list[NetworkInterfaceInfo] = Field(default_factory=list)
    system: SystemInfo | None = None
    # per-section error codes, e.g. {"gpu": "command_missing", "network": "timeout"}
    errors: dict[str, str] = Field(default_factory=dict)
    stale: bool = False


class FleetGpu(BaseModel):
    """Compact per-GPU lane for the fleet view (no processes, no history)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    utilization_percent: float | None = None
    vram_used_b: int | None = None
    vram_total_b: int | None = None
    temperature_c: float | None = None
    power_watts: float | None = None
    power_limit_watts: float | None = None
    users: list[str] = Field(default_factory=list)
    availability: GpuAvailability = GpuAvailability.UNAVAILABLE


class FleetEntry(BaseModel):
    """Low-cost per-server summary for the fleet view."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    display_name: str
    ssh_endpoint: str | None = None
    status: ServerStatus = ServerStatus.UNKNOWN
    enabled: bool = True
    os_pretty: str | None = None
    gpu_model: str | None = None
    gpu_count: int = 0
    gpu_busy: int = 0
    gpu_free: int = 0
    cpu_percent: float | None = None
    memory_percent: float | None = None
    vram_used_b: int | None = None
    vram_total_b: int | None = None
    disk_warning: bool = False
    gpus: list[FleetGpu] = Field(default_factory=list)
    updated_at: datetime | None = None


class FleetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    servers: list[FleetEntry] = Field(default_factory=list)


class HistoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: datetime
    v: float

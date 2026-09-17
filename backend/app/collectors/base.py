"""Collector boundary: typed, pure transformation of remote output.

A collector receives an Executor and returns typed models. It must never
import the SSH transport, never write to disk, never perform actions.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.models.telemetry import (
    CpuInfo,
    GpuInfo,
    GpuProcessInfo,
    MemoryInfo,
    NetworkInterfaceInfo,
    ProcessInfo,
    StorageMount,
    SystemInfo,
)
from app.ssh.executor import RemoteCommandResult


class CollectorError(Exception):
    """A collector failed for a classified reason (code drives UI state)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@runtime_checkable
class ExecutorLike(Protocol):
    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult: ...


class Collector(Protocol):
    """`name` matches section keys in ServerSnapshot.errors.

    `collect` may receive a pre-fetched `RemoteCommandResult`: the scheduler
    dedupes shared command specs (e.g. CPU_MEMORY serves cpu + memory) and
    passes each outcome in. It may also fetch on its own via the executor.
    """

    name: str

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> object: ...


# Section names used by scheduler / state / errors dict:
SECTION_GPU = "gpu"
SECTION_GPU_PROCESSES = "gpu_processes"
SECTION_CPU = "cpu"
SECTION_MEMORY = "memory"
SECTION_STORAGE = "storage"
SECTION_NETWORK = "network"
SECTION_PROCESSES = "processes"
SECTION_SYSTEM = "system"

__all__ = [
    "SECTION_CPU",
    "SECTION_GPU",
    "SECTION_GPU_PROCESSES",
    "SECTION_MEMORY",
    "SECTION_NETWORK",
    "SECTION_PROCESSES",
    "SECTION_STORAGE",
    "SECTION_SYSTEM",
    "Collector",
    "CollectorError",
    "CpuInfo",
    "ExecutorLike",
    "GpuInfo",
    "GpuProcessInfo",
    "MemoryInfo",
    "NetworkInterfaceInfo",
    "ProcessInfo",
    "StorageMount",
    "SystemInfo",
]

"""Process-wide runtime container (Sol-owned contract).

`main.py` constructs a Runtime at startup; API routes and services read it via
`get_runtime()`. Modules must not import runtime state at module import time —
always through the dependency, so tests can swap the container.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imports exist only for typing; nothing is imported at runtime
    from app.core.config import Settings
    from app.ssh.manager import SshManager
    from app.telemetry.service import TelemetryService
    from app.transfer.batch import BatchRegistry
    from app.transfer.service import TransferService
    from app.workspace.distribution import DistributionService
    from app.workspace.service import WorkspaceService


@dataclass(slots=True)
class Runtime:
    settings: Settings
    ssh: SshManager
    telemetry: TelemetryService
    workspace: WorkspaceService | None = None
    transfers: TransferService | None = None
    distribution: DistributionService | None = None
    batches: BatchRegistry | None = None


_runtime: Runtime | None = None


def set_runtime(runtime: Runtime | None) -> None:
    global _runtime
    _runtime = runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("runtime not initialised")
    return _runtime

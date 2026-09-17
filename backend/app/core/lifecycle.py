"""Composition root helpers: builds and runs the real application context.

The context wires registry → SSH manager → telemetry service. Tests inject
their own components; production uses `build()` with the user's OpenSSH
environment.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.runtime import Runtime, get_runtime, set_runtime
from app.ssh.manager import SshManager
from app.ssh.transport import build_executor
from app.telemetry.service import TelemetryService
from app.transfer.service import TransferService
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService

logger = get_logger("core.lifecycle")


class AppContext:
    """Owns the long-lived backend collaborators."""

    def __init__(
        self,
        settings: Settings,
        *,
        ssh: SshManager,
        telemetry: TelemetryService,
        workspace: WorkspaceService | None = None,
        transfers: TransferService | None = None,
    ) -> None:
        self.settings = settings
        self.ssh = ssh
        self.telemetry = telemetry
        self.workspace = workspace
        self.transfers = transfers

    async def start(self) -> None:
        await self.telemetry.start()
        reconcile = getattr(self.telemetry, "reconcile_servers", None)
        if callable(reconcile):
            from app.servers.registry import get_default_registry

            reconcile(get_default_registry().all())
        logger.info("application context started")

    async def stop(self) -> None:
        # Transfers first (they own SSH sessions), then telemetry, then SSH.
        if self.transfers is not None:
            await self.transfers.stop()
        await self.telemetry.stop()
        await self.ssh.close_all()
        logger.info("application context stopped")


def build_context(settings: Settings) -> AppContext:
    """Construct the production context over the user's real SSH environment."""
    from app.models.server import ServerRecord
    from app.servers.registry import get_default_registry
    from app.ssh.executor import Executor

    ssh = SshManager(settings)

    async def executor_factory(server_id: str, record: ServerRecord) -> Executor:
        return build_executor(ssh, record)

    telemetry = TelemetryService(settings, get_default_registry(), executor_factory)

    from app.persistence.json_store import JsonFileStore

    repository = WorkspaceRepository(JsonFileStore(settings.data_dir / "workspace.json"))
    workspace = WorkspaceService(repository, server_exists=_server_exists)

    from app.transfer.service import TransferService

    transfers = TransferService(settings=settings, ssh=ssh, workspace=workspace)
    return AppContext(
        settings,
        ssh=ssh,
        telemetry=telemetry,
        workspace=workspace,
        transfers=transfers,
    )


def _server_exists(server_id: str) -> bool:
    from app.core.errors import AppError
    from app.servers.registry import get_default_registry

    try:
        get_default_registry().get(server_id)
    except AppError:
        return False
    return True


def install_runtime(context: AppContext) -> Runtime:
    runtime = Runtime(
        settings=context.settings,
        ssh=context.ssh,
        telemetry=context.telemetry,
        workspace=context.workspace,  # type: ignore[arg-type]
        transfers=context.transfers,  # type: ignore[arg-type]
    )
    set_runtime(runtime)
    return runtime


async def notify_registry_changed() -> None:
    """CRUD hook: reconcile the scheduler with the current registry.

    Safe to call from any route: no-op when the runtime is absent (unit tests
    of the registry alone) or when telemetry is a test double without the
    reconcile interface.
    """

    try:
        runtime = get_runtime()
    except RuntimeError:
        return
    telemetry = getattr(runtime, "telemetry", None)
    reconcile = getattr(telemetry, "reconcile_servers", None)
    if not callable(reconcile):
        return
    from app.servers.registry import get_default_registry

    result = reconcile(get_default_registry().all())
    if hasattr(result, "__await__"):
        await result  # type: ignore[arg-type]

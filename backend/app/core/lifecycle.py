"""Composition root helpers: builds and runs the real application context.

The context wires registry → SSH manager → telemetry service. Tests inject
their own components; production uses `build()` with the user's OpenSSH
environment.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.logging import get_logger
from app.direct_auth.metadata import DirectAuthMetadata
from app.direct_auth.service import DirectAuthService
from app.runtime import Runtime, get_runtime, set_runtime
from app.ssh.manager import SshManager
from app.ssh.transport import build_executor
from app.telemetry.service import TelemetryService
from app.transfer.batch import BatchRegistry
from app.transfer.service import TransferService
from app.workspace.distribution import DistributionService, ServerUnavailableError
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from app.workspace.sync_planner import SyncPlanner

logger = get_logger("core.lifecycle")

# Sync planner installed by `install_runtime`; the workspace router resolves it
# through `get_sync_planner()` (Runtime itself is a frozen Sol-owned contract).
_sync_planner: SyncPlanner | None = None


def get_sync_planner() -> SyncPlanner | None:
    """Planner for the currently installed runtime (None in minimal contexts)."""
    return _sync_planner


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
        distribution: DistributionService | None = None,
        sync_planner: SyncPlanner | None = None,
        batches: BatchRegistry | None = None,
        direct_auth: DirectAuthService | None = None,
    ) -> None:
        self.settings = settings
        self.ssh = ssh
        self.telemetry = telemetry
        self.workspace = workspace
        self.transfers = transfers
        self.distribution = distribution
        self.sync_planner = sync_planner
        self.batches = batches
        self.direct_auth = direct_auth

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
    from app.credentials.store import build_credential_store, set_credential_store
    from app.models.server import ServerRecord
    from app.servers.registry import get_default_registry
    from app.ssh.executor import Executor

    # One credential store for the whole process (OS keyring when secure,
    # session RAM otherwise). Routes reach it via get_credential_store(); the
    # connect factory receives the same instance for password lookups.
    credential_store = build_credential_store()
    set_credential_store(credential_store)

    ssh = SshManager(settings, credentials=credential_store)

    async def executor_factory(server_id: str, record: ServerRecord) -> Executor:
        return build_executor(ssh, record)

    telemetry = TelemetryService(settings, get_default_registry(), executor_factory)

    from app.persistence.json_store import JsonFileStore

    repository = WorkspaceRepository(JsonFileStore(settings.data_dir / "workspace.json"))
    workspace = WorkspaceService(repository, server_exists=_server_exists)

    from app.transfer.service import TransferService

    direct_auth = DirectAuthService(
        settings,
        ssh,
        registry_lookup=lambda server_id: get_default_registry().get(server_id),
        workspace_like_server_lookup=_server_exists,
        metadata=DirectAuthMetadata(JsonFileStore(settings.data_dir / "direct_auth.json")),
    )

    transfers = TransferService(
        settings=settings, ssh=ssh, workspace=workspace, direct_auth=direct_auth
    )

    batches = BatchRegistry()

    def distribution_executor_for(server_id: str) -> Executor:
        """Executor factory for DistributionService: unknown/disabled servers
        are rejected before any executor exists; the service records those as
        UNAVAILABLE observations instead of touching SSH."""
        from app.core.errors import AppError
        from app.servers.registry import get_default_registry

        try:
            server = get_default_registry().get(server_id)
        except AppError:
            raise ServerUnavailableError("server is unknown, deleted or disabled") from None
        if not server.enabled:
            raise ServerUnavailableError("server is disabled")
        return build_executor(ssh, server)

    distribution = DistributionService(
        settings,
        workspace,
        executor_for=distribution_executor_for,
        active_transfers=lambda: transfers.list_jobs(),
    )
    sync_planner = SyncPlanner(
        workspace,
        distribution,
        executor_for=distribution_executor_for,
        plan_transfer=transfers.plan,
    )
    return AppContext(
        settings,
        ssh=ssh,
        telemetry=telemetry,
        workspace=workspace,
        transfers=transfers,
        distribution=distribution,
        sync_planner=sync_planner,
        batches=batches,
        direct_auth=direct_auth,
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
    global _sync_planner
    _sync_planner = context.sync_planner
    runtime = Runtime(
        settings=context.settings,
        ssh=context.ssh,
        telemetry=context.telemetry,
        workspace=context.workspace,  # type: ignore[arg-type]
        transfers=context.transfers,  # type: ignore[arg-type]
        distribution=context.distribution,  # type: ignore[arg-type]
        batches=context.batches,
        direct_auth=context.direct_auth,  # type: ignore[arg-type]
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

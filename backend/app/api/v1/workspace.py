"""Workspace routes: projects, artifacts, placements, distribution, roots.

Thin over WorkspaceService / DistributionService / SyncPlanner resolved through
the runtime. No endpoint accepts shell strings; launch configs are structured
data. All SSH work (placement checks, sync-plan source preflights) goes through
the runtime's services — routes never build executors themselves.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from app.core.errors import AppError, ConflictError
from app.core.lifecycle import get_sync_planner
from app.models.distribution import (
    ArtifactSyncItem,
    ProjectDistribution,
    ProjectSyncPlan,
    SyncAction,
    SyncPlanRequest,
)
from app.models.transfer import TransferBatch, TransferRequest
from app.models.workspace import (
    ArtifactCreate,
    ArtifactPatch,
    ArtifactRecord,
    LaunchConfigCreate,
    LaunchConfigPatch,
    LaunchConfigRecord,
    PlacementCreate,
    PlacementInspection,
    PlacementPatch,
    PlacementRecord,
    ProjectCreate,
    ProjectPatch,
    ProjectRecord,
    ServerRoots,
    ServerRootsUpdate,
)
from app.runtime import get_runtime
from app.servers.registry import get_default_registry

if TYPE_CHECKING:
    from app.transfer.batch import BatchRegistry
    from app.transfer.service import TransferService
    from app.workspace.distribution import DistributionService
    from app.workspace.service import WorkspaceService
    from app.workspace.sync_planner import SyncPlanner

router = APIRouter(prefix="/api/v1/workspace", tags=["workspace"])


def _service() -> WorkspaceService:
    service = get_runtime().workspace
    if service is None:  # runtime built without workspace (unit tests)
        raise RuntimeError("workspace service unavailable")
    return service


def _distribution() -> DistributionService:
    service = get_runtime().distribution
    if service is None:  # runtime built without distribution (unit tests)
        raise RuntimeError("distribution service unavailable")
    return service


def _sync_planner() -> SyncPlanner:
    planner = get_sync_planner()
    if planner is None:  # runtime built without the sync planner (unit tests)
        raise RuntimeError("sync plan service unavailable")
    return planner


def _batches() -> BatchRegistry:
    registry = get_runtime().batches
    if registry is None:  # runtime built without the batch registry (unit tests)
        raise RuntimeError("transfer batch registry unavailable")
    return registry


def _transfers() -> TransferService:
    service = get_runtime().transfers
    if service is None:  # runtime built without transfers (unit tests)
        raise RuntimeError("transfer service unavailable")
    return service


# ---- projects ------------------------------------------------------------------


@router.get("/projects", response_model=list[ProjectRecord])
def list_projects() -> list[ProjectRecord]:
    return _service().projects()


@router.post("/projects", response_model=ProjectRecord, status_code=201)
def create_project(request: ProjectCreate) -> ProjectRecord:
    return _service().create_project(request)


@router.patch("/projects/{project_id}", response_model=ProjectRecord)
def update_project(project_id: str, request: ProjectPatch) -> ProjectRecord:
    return _service().update_project(project_id, request)


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str) -> None:
    _service().delete_project(project_id)


# ---- project distribution --------------------------------------------------------


@router.get("/projects/{project_id}/distribution", response_model=ProjectDistribution)
def project_distribution(project_id: str) -> ProjectDistribution:
    # Zero-SSH read model: declarations + cached observations + active transfers.
    return _distribution().distribution(project_id)


@router.post("/projects/{project_id}/inspect", response_model=ProjectDistribution)
async def inspect_project(project_id: str) -> ProjectDistribution:
    return await _distribution().inspect_project(project_id)


@router.post("/projects/{project_id}/sync-plan", response_model=ProjectSyncPlan)
async def sync_plan(project_id: str, request: SyncPlanRequest) -> ProjectSyncPlan:
    # Explicit project-level reconciliation plan toward one target server;
    # bounded SSH happens inside the planner (declared-target checks, source
    # preflights), never here.
    return await _sync_planner().build_plan(project_id, request)


@router.post("/projects/{project_id}/sync", response_model=TransferBatch)
async def sync_project(project_id: str, request: SyncPlanRequest) -> TransferBatch:
    """Execute a project sync toward one target server.

    The plan is ALWAYS regenerated here (build_plan) — a plan is never
    accepted from the client. UNRESOLVED items or an invalid plan (target
    overlap) refuse the whole sync with 409 before anything is created; a
    plan without TRANSFER items yields a legal empty batch. Each TRANSFER
    item becomes one real TransferJob through TransferService.create; the
    batch only groups their ids and derives its state from the real jobs.
    """
    _require_enabled_target(request.target_server_id)
    batches = _batches()
    transfers = _transfers()
    plan = await _sync_planner().build_plan(project_id, request)
    unresolved = [item for item in plan.items if item.action is SyncAction.UNRESOLVED]
    if unresolved or not plan.valid:
        raise ConflictError(_rejection_message(plan, unresolved))

    def create_job(item: ArtifactSyncItem) -> str:
        if (
            item.source_placement_id is None
            or item.target_path is None
            or item.strategy_selected is None
        ):
            raise ConflictError(
                f"transfer item for {item.artifact_label!r} is incomplete:"
                " missing source placement, target path or strategy"
            )
        created = transfers.create(
            TransferRequest(
                artifact_id=item.artifact_id,
                source_placement_id=item.source_placement_id,
                target_server_id=plan.target_server_id,
                target_path=item.target_path,
                strategy=item.strategy_selected,
            )
        )
        return created["job_id"]

    return await batches.start(
        project_id=project_id,
        target_server_id=plan.target_server_id,
        transfer_items=[item for item in plan.items if item.action is SyncAction.TRANSFER],
        create_job=create_job,
    )


def _require_enabled_target(server_id: str) -> None:
    """Unknown or disabled target -> 409: a sync can never be executed there."""
    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise ConflictError(f"target server {server_id!r} is unknown or deleted") from error
    if not server.enabled:
        raise ConflictError(f"target server {server_id!r} is disabled")


def _rejection_message(plan: ProjectSyncPlan, unresolved: list[ArtifactSyncItem]) -> str:
    lines = [f"{item.artifact_label}: {item.reason}" for item in unresolved]
    if not plan.valid and plan.error:
        lines.append(plan.error)  # the planner's overlap error, verbatim
    return ("sync refused, no transfer started: " + "; ".join(lines))[:800]


# ---- artifacts -----------------------------------------------------------------


@router.get("/artifacts", response_model=list[ArtifactRecord])
def list_artifacts(kind: str | None = None) -> list[ArtifactRecord]:
    return _service().artifacts(kind)


@router.post("/artifacts", response_model=ArtifactRecord, status_code=201)
def create_artifact(request: ArtifactCreate) -> ArtifactRecord:
    return _service().create_artifact(request)


@router.patch("/artifacts/{artifact_id}", response_model=ArtifactRecord)
def update_artifact(artifact_id: str, request: ArtifactPatch) -> ArtifactRecord:
    return _service().update_artifact(artifact_id, request)


@router.delete("/artifacts/{artifact_id}", status_code=204)
def delete_artifact(artifact_id: str) -> None:
    _service().delete_artifact(artifact_id)


# ---- placements ----------------------------------------------------------------


@router.get("/placements", response_model=list[PlacementRecord])
def list_placements() -> list[PlacementRecord]:
    return _service().placements()


@router.post("/placements", response_model=PlacementRecord, status_code=201)
def create_placement(request: PlacementCreate) -> PlacementRecord:
    return _service().create_placement(request)


@router.patch("/placements/{placement_id}", response_model=PlacementRecord)
def update_placement(placement_id: str, request: PlacementPatch) -> PlacementRecord:
    return _service().update_placement(placement_id, request)


@router.delete("/placements/{placement_id}", status_code=204)
def delete_placement(placement_id: str) -> None:
    _service().delete_placement(placement_id)


@router.post("/placements/{placement_id}/inspect", response_model=PlacementInspection)
async def inspect_placement(placement_id: str) -> PlacementInspection:
    # Unknown/disabled servers inspect as UNAVAILABLE inside the service
    # (executor factory rejects them), not as 404 — the placement exists;
    # only the remote check is impossible.
    return await _distribution().inspect_placement(placement_id)


# ---- launch configs ------------------------------------------------------------


@router.get("/launch-configs", response_model=list[LaunchConfigRecord])
def list_launch_configs() -> list[LaunchConfigRecord]:
    return _service().launch_configs()


@router.post("/launch-configs", response_model=LaunchConfigRecord, status_code=201)
def create_launch_config(request: LaunchConfigCreate) -> LaunchConfigRecord:
    return _service().create_launch_config(request)


@router.patch("/launch-configs/{launch_config_id}", response_model=LaunchConfigRecord)
def update_launch_config(launch_config_id: str, request: LaunchConfigPatch) -> LaunchConfigRecord:
    return _service().update_launch_config(launch_config_id, request)


@router.delete("/launch-configs/{launch_config_id}", status_code=204)
def delete_launch_config(launch_config_id: str) -> None:
    _service().delete_launch_config(launch_config_id)


# ---- server roots --------------------------------------------------------------


@router.get("/server-roots/{server_id}", response_model=ServerRoots)
def get_server_roots(server_id: str) -> ServerRoots:
    roots = _service().server_roots(server_id)
    return roots if roots is not None else ServerRoots()


@router.put("/server-roots/{server_id}", response_model=ServerRoots)
def set_server_roots(server_id: str, request: ServerRootsUpdate) -> ServerRoots:
    return _service().set_server_roots(server_id, request)

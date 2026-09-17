"""Workspace routes: projects, artifacts, placements, launch configs, roots.

Thin over WorkspaceService / PlacementInspector resolved through the runtime.
No endpoint accepts shell strings; launch configs are structured data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from app.models.workspace import (
    ArtifactCreate,
    ArtifactPatch,
    ArtifactRecord,
    InspectionState,
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
from app.ssh.transport import build_executor
from app.workspace.inspection import PlacementInspector

if TYPE_CHECKING:
    from app.servers.registry import ServerRegistry
    from app.workspace.service import WorkspaceService

router = APIRouter(prefix="/api/v1/workspace", tags=["workspace"])

_inspector = PlacementInspector()


def _service() -> WorkspaceService:
    service = get_runtime().workspace
    if service is None:  # runtime built without workspace (unit tests)
        raise RuntimeError("workspace service unavailable")
    return service


def _server_exists(server_id: str) -> bool:
    try:
        _registry().get(server_id)
    except Exception:
        return False
    return True


def _registry() -> ServerRegistry:
    from app.servers.registry import get_default_registry

    return get_default_registry()


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
    placement = _service().placement(placement_id)
    try:
        server = _registry().get(placement.server_id)
    except Exception:
        # Unknown/disabled servers inspect as unavailable, not 404 — the
        # placement itself exists; only the remote check is impossible.
        return PlacementInspection(
            placement_id=placement_id,
            state=InspectionState.UNAVAILABLE,
            detail="server is unknown, deleted or disabled",
        )
    if not server.enabled:
        return PlacementInspection(
            placement_id=placement_id,
            state=InspectionState.UNAVAILABLE,
            detail="server is disabled",
        )
    executor = build_executor(get_runtime().ssh, server)
    return await _inspector.inspect(executor, placement)


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

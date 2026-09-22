"""Workspace domain service: CRUD orchestration with domain validation.

Routes never touch the repository directly. SSH access lives in
inspection.py / transfer; this module is sync and cheap.
"""

from __future__ import annotations

from collections.abc import Callable

from app.core.errors import ConflictError, NotFoundError
from app.models.workspace import (
    ArtifactCreate,
    ArtifactPatch,
    ArtifactRecord,
    LaunchConfigCreate,
    LaunchConfigPatch,
    LaunchConfigRecord,
    PlacementCreate,
    PlacementPatch,
    PlacementRecord,
    ProjectCreate,
    ProjectPatch,
    ProjectRecord,
    ServerRoots,
    ServerRootsUpdate,
)
from app.workspace.paths import claim_path, normalize_remote_path, root_for_kind, safe_artifact_leaf
from app.workspace.repository import WorkspaceRepository, new_id, utc_now

ServerLookup = Callable[[str], bool]  # registry.contains(server_id) -> bool


class WorkspaceService:
    """Validated operations over projects, artifacts, placements, launch configs."""

    def __init__(self, repository: WorkspaceRepository, server_exists: ServerLookup) -> None:
        self._repo = repository
        self._server_exists = server_exists

    # ---- projects -----------------------------------------------------------

    def create_project(self, request: ProjectCreate) -> ProjectRecord:
        record = ProjectRecord(
            project_id=new_id(),
            name=request.name.strip(),
            description=request.description.strip(),
            artifact_ids=list(dict.fromkeys(request.artifact_ids)),
            tags=list(request.tags),
            transfer_excludes=request.transfer_excludes,
            created_at=_stamp(),
            updated_at=_stamp(),
        )
        return self._with_launch_config_ids(self._repo.insert_project(record))

    def update_project(self, project_id: str, request: ProjectPatch) -> ProjectRecord:
        current = self._repo.get_project(project_id)
        patch = request.model_dump(exclude_unset=True)
        if not patch:
            raise ConflictError("empty update")
        updated = ProjectRecord.model_validate(
            {**current.model_dump(mode="python"), **patch, "updated_at": _stamp()}
        )
        return self._with_launch_config_ids(self._repo.replace_project(updated))

    def delete_project(self, project_id: str) -> None:
        # Metadata-only: artifacts and placements are untouched; launch configs
        # (project-owned config) cascade; remote files are never touched.
        self._repo.remove_project(project_id)

    def projects(self) -> list[ProjectRecord]:
        return [self._with_launch_config_ids(p) for p in self._repo.projects()]

    def project(self, project_id: str) -> ProjectRecord:
        return self._with_launch_config_ids(self._repo.get_project(project_id))

    def _with_launch_config_ids(self, project: ProjectRecord) -> ProjectRecord:
        """launch_config_ids is DERIVED, never stored: the launch config owns
        the project_id association, so the project row count stays true without
        a second write path that can go stale."""
        derived = [
            config.launch_config_id
            for config in self._repo.launch_configs()
            if config.project_id == project.project_id
        ]
        if derived == project.launch_config_ids:
            return project
        return ProjectRecord.model_validate(
            {**project.model_dump(mode="python"), "launch_config_ids": derived}
        )

    # ---- artifacts ----------------------------------------------------------

    def create_artifact(self, request: ArtifactCreate) -> ArtifactRecord:
        immutable = request.immutable if request.immutable is not None else request.kind != "code"
        record = ArtifactRecord(
            artifact_id=new_id(),
            kind=request.kind,
            name=request.name.strip(),
            version=(request.version.strip() or None) if request.version else None,
            description=request.description.strip(),
            immutable=immutable,
            created_at=_stamp(),
            updated_at=_stamp(),
        )
        return self._repo.insert_artifact(record)

    def update_artifact(self, artifact_id: str, request: ArtifactPatch) -> ArtifactRecord:
        current = self._repo.get_artifact(artifact_id)
        patch = request.model_dump(exclude_unset=True)
        if not patch:
            raise ConflictError("no changes")
        updated = ArtifactRecord.model_validate(
            {**current.model_dump(mode="python"), **patch, "updated_at": _stamp()}
        )
        return self._repo.replace_artifact(updated)

    def delete_artifact(self, artifact_id: str) -> None:
        self._repo.remove_artifact(artifact_id)

    def artifacts(self, kind: str | None = None) -> list[ArtifactRecord]:
        return self._repo.artifacts_by_kind(kind) if kind else self._repo.artifacts()

    def artifact(self, artifact_id: str) -> ArtifactRecord:
        return self._repo.get_artifact(artifact_id)

    def placements_of(self, artifact_id: str) -> list[PlacementRecord]:
        return self._repo.placements_of(artifact_id)

    # ---- placements ---------------------------------------------------------

    def create_placement(self, request: PlacementCreate) -> PlacementRecord:
        if not self._server_exists(request.server_id):
            raise NotFoundError(f"server {request.server_id!r} not found")
        self._repo.get_artifact(request.artifact_id)  # must exist
        existing = self._repo.placements()
        duplicate = any(
            p.artifact_id == request.artifact_id
            and p.server_id == request.server_id
            and p.remote_path == request.remote_path.strip()
            for p in existing
        )
        if duplicate:
            raise ConflictError("identical placement already exists")
        record = PlacementRecord(
            placement_id=new_id(),
            artifact_id=request.artifact_id,
            server_id=request.server_id,
            remote_path=request.remote_path.strip(),
            created_at=_stamp(),
            updated_at=_stamp(),
        )
        return self._repo.insert_placement(record)

    def update_placement(self, placement_id: str, request: PlacementPatch) -> PlacementRecord:
        current = self._repo.get_placement(placement_id)
        patch = request.model_dump(exclude_unset=True)
        if not patch:
            raise ConflictError("no changes")
        updated = PlacementRecord.model_validate(
            {**current.model_dump(mode="python"), **patch, "updated_at": _stamp()}
        )
        return self._repo.replace_placement(updated)

    def delete_placement(self, placement_id: str) -> None:
        # Metadata-only: the remote directory is NEVER removed here.
        self._repo.remove_placement(placement_id)

    def placements(self) -> list[PlacementRecord]:
        return self._repo.placements()

    def placement(self, placement_id: str) -> PlacementRecord:
        return self._repo.get_placement(placement_id)

    # ---- launch configs -------------------------------------------------------

    def create_launch_config(self, request: LaunchConfigCreate) -> LaunchConfigRecord:
        self._repo.get_project(request.project_id)
        self._reject_unknown_artifacts(request.required_artifact_ids)
        record = LaunchConfigRecord(
            launch_config_id=new_id(),
            project_id=request.project_id,
            name=request.name.strip(),
            working_dir=request.working_dir,
            program=request.program.strip(),
            args=list(request.args),
            environment=request.environment,
            env_vars=dict(request.env_vars),
            required_artifact_ids=list(dict.fromkeys(request.required_artifact_ids)),
            gpu_count=request.gpu_count,
            min_vram_b=request.min_vram_b,
            created_at=_stamp(),
            updated_at=_stamp(),
        )
        return self._repo.insert_launch_config(record)

    def update_launch_config(
        self, launch_config_id: str, request: LaunchConfigPatch
    ) -> LaunchConfigRecord:
        current = self._repo.get_launch_config(launch_config_id)
        patch = request.model_dump(exclude_unset=True)
        if not patch:
            raise ConflictError("no changes")
        updated = LaunchConfigRecord.model_validate(
            {**current.model_dump(mode="python"), **patch, "updated_at": _stamp()}
        )
        self._reject_unknown_artifacts(updated.required_artifact_ids)
        return self._repo.replace_launch_config(updated)

    def delete_launch_config(self, launch_config_id: str) -> None:
        self._repo.remove_launch_config(launch_config_id)

    def launch_configs(self) -> list[LaunchConfigRecord]:
        return self._repo.launch_configs()

    def launch_config(self, launch_config_id: str) -> LaunchConfigRecord:
        return self._repo.get_launch_config(launch_config_id)

    # ---- server roots ---------------------------------------------------------

    def server_roots(self, server_id: str) -> ServerRoots | None:
        if not self._server_exists(server_id):
            raise NotFoundError(f"server {server_id!r} not found")
        return self._repo.server_roots(server_id)

    def set_server_roots(self, server_id: str, update: ServerRootsUpdate) -> ServerRoots:
        if not self._server_exists(server_id):
            raise NotFoundError(f"server {server_id!r} not found")
        roots = ServerRoots(
            project_root=update.project_root,
            dataset_root=update.dataset_root,
            model_root=update.model_root,
            output_root=update.output_root,
        )
        return self._repo.set_server_roots(server_id, roots)

    def suggest_target_path(
        self, server_id: str, artifact_id: str
    ) -> tuple[str | None, str | None]:
        """Canonical target-path suggestion: (path, reason).

        Built exactly like Project Sync's automatic suggestion (the same
        ``safe_artifact_leaf`` + collision escalation helpers), so a manual
        transfer pre-fills the same filesystem-safe naming a sync plan would
        use. Deterministic: repeated calls with identical inputs return the
        identical path.

        - unknown artifact / unknown server: the repo's and service's own
          NotFoundError surfaces (404 at the route layer);
        - no roots record for the server, or the kind's root missing/empty:
          ``(None, "server_root_not_configured")``;
        - a display name with no usable path characters (sanitize_component's
          ConflictError): ``(None, "artifact_name_unusable")``;
        - success: ``(path, None)`` with the base path claimed against the
          placements of OTHER artifacts on THIS server (``--`` + id-prefix
          escalation, never a hash).
        """
        artifact = self._repo.get_artifact(artifact_id)  # NotFoundError -> 404
        roots = self.server_roots(server_id)  # unknown server -> NotFoundError
        root = root_for_kind(roots, artifact.kind) if roots is not None else None
        if not root:
            return None, "server_root_not_configured"
        try:
            leaf = safe_artifact_leaf(artifact.name, artifact.version, artifact.artifact_id)
            base = normalize_remote_path(f"{root.rstrip('/')}/{leaf}")
        except ConflictError:
            return None, "artifact_name_unusable"
        taken = {
            normalize_remote_path(placement.remote_path)
            for placement in self._repo.placements()
            if placement.server_id == server_id and placement.artifact_id != artifact_id
        }
        return claim_path(base, artifact_id, taken), None

    def _reject_unknown_artifacts(self, artifact_ids: list[str]) -> None:
        existing = {artifact.artifact_id for artifact in self._repo.artifacts()}
        missing = [artifact_id for artifact_id in artifact_ids if artifact_id not in existing]
        if missing:
            raise ConflictError(f"unknown artifact reference: {missing[0]}")


def _stamp() -> str:
    return utc_now()

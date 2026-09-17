"""Workspace repository: load/CRUD/save over one atomic JSON document.

Pure persistence + referential integrity. Domain validation lives in the
service; this module never talks SSH and writes only on explicit mutations.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.workspace import (
    ArtifactRecord,
    LaunchConfigRecord,
    PlacementRecord,
    ProjectRecord,
    ServerRoots,
)
from app.persistence.json_store import JsonFileStore

logger = get_logger("workspace.repository")

_SCHEMA_VERSION = 1
_MAX_PER_KIND = 512

_MODEL_BY_KEY: dict[str, type[BaseModel]] = {
    "projects": ProjectRecord,
    "artifacts": ArtifactRecord,
    "placements": PlacementRecord,
    "launch_configs": LaunchConfigRecord,
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _id_of(key: str) -> str:
    return f"{key[:-1]}_id"


class WorkspaceRepository:
    """In-memory workspace declarations with atomic JSON persistence."""

    def __init__(self, store: JsonFileStore) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._projects: dict[str, ProjectRecord] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._placements: dict[str, PlacementRecord] = {}
        self._launch_configs: dict[str, LaunchConfigRecord] = {}
        self._server_roots: dict[str, ServerRoots] = {}
        self._load()

    # ---- persistence ---------------------------------------------------------

    def _load(self) -> None:
        raw: Any = self._store.load(default=None)
        if not isinstance(raw, dict):
            logger.warning("workspace store missing/corrupt; starting empty")
            return
        for key, model in _MODEL_BY_KEY.items():
            for item in raw.get(key, []):
                if not isinstance(item, dict):
                    continue
                try:
                    record = model.model_validate(item)
                except Exception:
                    logger.warning("skipping invalid workspace %s record", key)
                    continue
                getattr(self, f"_{key}")[getattr(record, _id_of(key))] = record
        for server_id, roots in raw.get("server_roots", {}).items():
            if not (isinstance(server_id, str) and server_id and isinstance(roots, dict)):
                continue
            try:
                self._server_roots[server_id] = ServerRoots.model_validate(roots)
            except Exception:
                logger.warning("skipping invalid server roots for %s", server_id)

    def _save_locked(self) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "projects": [p.model_dump(mode="json") for p in self._projects.values()],
            "artifacts": [a.model_dump(mode="json") for a in self._artifacts.values()],
            "placements": [p.model_dump(mode="json") for p in self._placements.values()],
            "launch_configs": [c.model_dump(mode="json") for c in self._launch_configs.values()],
            "server_roots": {
                server_id: roots.model_dump(mode="json")
                for server_id, roots in self._server_roots.items()
            },
        }
        self._store.save(payload)

    # ---- accessors -----------------------------------------------------------

    def projects(self) -> list[ProjectRecord]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self._projects.values()]

    def artifacts(self) -> list[ArtifactRecord]:
        with self._lock:
            return [a.model_copy(deep=True) for a in self._artifacts.values()]

    def placements(self) -> list[PlacementRecord]:
        with self._lock:
            return [p.model_copy(deep=True) for p in self._placements.values()]

    def launch_configs(self) -> list[LaunchConfigRecord]:
        with self._lock:
            return [c.model_copy(deep=True) for c in self._launch_configs.values()]

    def get_project(self, project_id: str) -> ProjectRecord:
        return self._get(self._projects, project_id, "project")

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        return self._get(self._artifacts, artifact_id, "artifact")

    def get_placement(self, placement_id: str) -> PlacementRecord:
        return self._get(self._placements, placement_id, "placement")

    def get_launch_config(self, launch_config_id: str) -> LaunchConfigRecord:
        return self._get(self._launch_configs, launch_config_id, "launch config")

    def server_roots(self, server_id: str) -> ServerRoots | None:
        with self._lock:
            roots = self._server_roots.get(server_id)
        return roots.model_copy(deep=True) if roots else None

    def set_server_roots(self, server_id: str, roots: ServerRoots) -> ServerRoots:
        with self._lock:
            self._server_roots[server_id] = roots
            self._save_locked()
        return roots.model_copy(deep=True)

    def placements_of(self, artifact_id: str) -> list[PlacementRecord]:
        with self._lock:
            return [
                p.model_copy(deep=True)
                for p in self._placements.values()
                if p.artifact_id == artifact_id
            ]

    def artifacts_by_kind(self, kind: str) -> list[ArtifactRecord]:
        with self._lock:
            return [a.model_copy(deep=True) for a in self._artifacts.values() if a.kind == kind]

    # ---- mutation helpers (service validates domain rules first) --------------

    def insert_project(self, record: ProjectRecord) -> ProjectRecord:
        with self._lock:
            self._check_project_refs(record)
            self._reject_duplicate_name(record.name, None)
            self._projects[record.project_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def replace_project(self, record: ProjectRecord) -> ProjectRecord:
        with self._lock:
            self._check_project_refs(record)
            self._reject_duplicate_name(record.name, record.project_id)
            self._projects[record.project_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def remove_project(self, project_id: str) -> None:
        with self._lock:
            if project_id not in self._projects:
                raise NotFoundError(f"project {project_id!r} not found")
            del self._projects[project_id]
            # Launch configs are project-owned configuration data; cascade them.
            for config_id in [
                cid
                for cid, config in self._launch_configs.items()
                if config.project_id == project_id
            ]:
                del self._launch_configs[config_id]
            self._save_locked()

    def insert_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        with self._lock:
            count = sum(1 for artifact in self._artifacts.values() if artifact.kind == record.kind)
            if count >= _MAX_PER_KIND:
                raise ConflictError(f"too many {record.kind} artifacts (limit {_MAX_PER_KIND})")
            self._artifacts[record.artifact_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def replace_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        with self._lock:
            self._artifacts[record.artifact_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def remove_artifact(self, artifact_id: str) -> None:
        with self._lock:
            if artifact_id not in self._artifacts:
                raise NotFoundError(f"artifact {artifact_id!r} not found")
            if any(artifact_id in p.artifact_ids for p in self._projects.values()):
                raise ConflictError("artifact is referenced by a project")
            if any(p.artifact_id == artifact_id for p in self._placements.values()):
                raise ConflictError("artifact has placements; delete them first")
            del self._artifacts[artifact_id]
            self._save_locked()

    def insert_placement(self, record: PlacementRecord) -> PlacementRecord:
        with self._lock:
            if record.artifact_id not in self._artifacts:
                raise ConflictError("artifact does not exist")
            self._placements[record.placement_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def replace_placement(self, record: PlacementRecord) -> PlacementRecord:
        with self._lock:
            self._placements[record.placement_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def remove_placement(self, placement_id: str) -> None:
        with self._lock:
            if placement_id not in self._placements:
                raise NotFoundError(f"placement {placement_id!r} not found")
            del self._placements[placement_id]
            self._save_locked()

    def insert_launch_config(self, record: LaunchConfigRecord) -> LaunchConfigRecord:
        with self._lock:
            self._launch_configs[record.launch_config_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def replace_launch_config(self, record: LaunchConfigRecord) -> LaunchConfigRecord:
        with self._lock:
            self._launch_configs[record.launch_config_id] = record
            self._save_locked()
            return record.model_copy(deep=True)

    def remove_launch_config(self, launch_config_id: str) -> None:
        with self._lock:
            if launch_config_id not in self._launch_configs:
                raise NotFoundError(f"launch config {launch_config_id!r} not found")
            del self._launch_configs[launch_config_id]
            self._save_locked()

    # ---- internal --------------------------------------------------------------

    def _get(self, bucket: dict[str, Any], record_id: str, label: str) -> Any:
        with self._lock:
            record = bucket.get(record_id)
        if record is None:
            raise NotFoundError(f"{label} {record_id!r} not found")
        return record.model_copy(deep=True)

    def _check_project_refs(self, record: ProjectRecord) -> None:
        missing = [aid for aid in record.artifact_ids if aid not in self._artifacts]
        if missing:
            raise ConflictError(f"unknown artifact reference: {missing[0]}")

    def _reject_duplicate_name(self, name: str, exclude_id: str | None) -> None:
        if any(p.name == name and p.project_id != exclude_id for p in self._projects.values()):
            raise ConflictError(f"project name {name!r} already exists")

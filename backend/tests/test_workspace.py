"""Workspace Phase 1 tests: repository, service, API (per WORKSPACE_SYNC_PLAN).

Fake SSH layer for inspection; no real server needed. The registry dependency
is swapped with a synthetic one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pydantic
import pytest
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.workspace import (
    ArtifactCreate,
    LaunchConfigCreate,
    PlacementCreate,
    ProjectCreate,
    ProjectPatch,
    ServerRootsUpdate,
)
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from app.telemetry.service import TelemetryService
from app.workspace.distribution import DistributionService, ServerUnavailableError
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from fastapi.testclient import TestClient


def make_service(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceRepository]:
    repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
    service = WorkspaceService(repo, server_exists=lambda sid: sid != "unknown")
    return service, repo


class FakeExecutor:
    def __init__(self, stdout: str = "", exit_code: int = 0) -> None:
        self.stdout = stdout
        self.exit_code = exit_code

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        return RemoteCommandResult(self.exit_code, self.stdout, "", 1.0)

    async def close(self) -> None:
        return None


class FakeSsh:
    async def run(
        self, _server: Any, _command: str, *, timeout_s: float | None = None
    ) -> RemoteCommandResult:
        return RemoteCommandResult(0, "", "", 1.0)

    async def close_server(self, _server_id: str) -> None:
        return None

    async def close_all(self) -> None:
        return None


# ---- repository / service -----------------------------------------------------


def test_empty_store_create_and_reload(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    assert service.projects() == []
    service.create_project(ProjectCreate(name="CMOS", description="RGB-IR open-set segmentation"))
    repo2 = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
    assert [p.name for p in repo2.projects()] == ["CMOS"]


def test_duplicate_project_name_rejected(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    service.create_project(ProjectCreate(name="CMOS"))
    with pytest.raises(ConflictError):
        service.create_project(ProjectCreate(name="CMOS"))


def test_project_unknown_artifact_reference(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    with pytest.raises(ConflictError):
        service.create_project(ProjectCreate(name="CMOS", artifact_ids=["missing-artifact"]))


def test_artifact_defaults_and_delete_rules(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    dataset = service.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD", version="v1"))
    code = service.create_artifact(ArtifactCreate(kind="code", name="CMOS-code"))
    assert dataset.immutable is True
    assert code.immutable is False

    service.create_project(ProjectCreate(name="P", artifact_ids=[dataset.artifact_id]))
    with pytest.raises(ConflictError):
        service.delete_artifact(dataset.artifact_id)  # referenced by project
    placement = service.create_placement(
        PlacementCreate(artifact_id=code.artifact_id, server_id="srv-a", remote_path="~/code/CMOS")
    )
    with pytest.raises(ConflictError):
        service.delete_artifact(code.artifact_id)  # has a placement
    service.delete_placement(placement.placement_id)  # metadata-only delete
    service.delete_artifact(code.artifact_id)  # now unreferenced: allowed


def test_placement_rules(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    artifact = service.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD"))
    with pytest.raises((NotFoundError, ConflictError)):
        service.create_placement(
            PlacementCreate(artifact_id=artifact.artifact_id, server_id="unknown", remote_path="/x")
        )
    placement = service.create_placement(
        PlacementCreate(
            artifact_id=artifact.artifact_id, server_id="srv-a", remote_path="/data/IVMSD"
        )
    )
    with pytest.raises(ConflictError):
        service.create_placement(
            PlacementCreate(
                artifact_id=artifact.artifact_id,
                server_id="srv-a",
                remote_path="/data/IVMSD",
            )
        )
    service.delete_placement(placement.placement_id)
    # delete is metadata-only: the same placement is allowed again
    service.create_placement(
        PlacementCreate(
            artifact_id=artifact.artifact_id, server_id="srv-a", remote_path="/data/IVMSD"
        )
    )


def test_launch_config_structured_and_cascades(tmp_path: Path) -> None:
    import pydantic

    service, _repo = make_service(tmp_path)
    artifact = service.create_artifact(ArtifactCreate(kind="model", name="DINOv2-B"))
    project = service.create_project(
        ProjectCreate(name="CMOS", artifact_ids=[artifact.artifact_id])
    )
    with pytest.raises(pydantic.ValidationError):
        service.create_launch_config(
            LaunchConfigCreate(
                project_id=project.project_id,
                name="x",
                program="cd /x && source env && python train.py",
                required_artifact_ids=[artifact.artifact_id],
            )
        )
    config = service.create_launch_config(
        LaunchConfigCreate(
            project_id=project.project_id,
            name="train",
            program="python",
            args=["-u", "train.py"],
            required_artifact_ids=[artifact.artifact_id],
            gpu_count=2,
        )
    )
    assert config.program == "python" and config.args == ["-u", "train.py"]

    with pytest.raises(ConflictError):
        service.create_launch_config(
            LaunchConfigCreate(
                project_id=project.project_id,
                name="y",
                program="python",
                required_artifact_ids=["ghost-artifact"],
            )
        )
    service.delete_project(project.project_id)
    assert service.launch_configs() == []  # cascade on project delete


def test_project_launch_config_ids_are_derived(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    artifact = service.create_artifact(ArtifactCreate(kind="model", name="DINOv2-B"))
    project = service.create_project(ProjectCreate(name="CMOS"))
    assert project.launch_config_ids == []

    config = service.create_launch_config(
        LaunchConfigCreate(
            project_id=project.project_id,
            name="train",
            program="python",
            required_artifact_ids=[artifact.artifact_id],
        )
    )
    # The count on the project row must reflect reality without a second
    # write path (bug: launch configs existed but projects showed 0).
    assert service.project(project.project_id).launch_config_ids == [config.launch_config_id]
    assert service.projects()[0].launch_config_ids == [config.launch_config_id]
    assert service.update_project(
        project.project_id, ProjectPatch(name="CMOS v2")
    ).launch_config_ids == [config.launch_config_id]

    service.delete_launch_config(config.launch_config_id)
    assert service.project(project.project_id).launch_config_ids == []


def test_server_roots_and_suggestion(tmp_path: Path) -> None:
    service, repo = make_service(tmp_path)
    service.set_server_roots("srv-a", ServerRootsUpdate(dataset_root="/DataDisk/datasets"))
    assert repo.server_roots("srv-a") is not None
    artifact = service.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD", version="v1"))
    assert (
        service.suggest_target_path("srv-a", artifact.artifact_id) == "/DataDisk/datasets/IVMSD:v1"
    )


# ---- API -----------------------------------------------------------------------


class Harness:
    def __init__(self, tmp_path: Path, servers: list[str]) -> None:
        self.settings = Settings(
            data_dir=tmp_path / "data",
            fleet_interval_active=1.0,
            fleet_interval_idle=2.0,
        )
        self.registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
        self.workspace_repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
        self.workspace = WorkspaceService(
            self.workspace_repo,
            server_exists=lambda sid: sid in self._names() or sid in self._ids(),
        )
        for server_id in servers:
            from app.models.server import ServerCreate

            self.registry.create(
                ServerCreate(display_name=server_id, ssh_host=f"{server_id}.example")
            )
        set_default_registry(self.registry)
        self.client = TestClient(create_app(self.settings, context=self._context()))

    def _names(self) -> set[str]:
        return {server.display_name for server in self.registry.all()}

    def _executor_for(self, server_id: str) -> FakeExecutor:
        """DistributionService factory double: unknown servers raise, like the
        production factory; known servers get an (empty-output) fake executor."""
        if server_id not in self._names() and server_id not in self._ids():
            raise ServerUnavailableError("server is unknown, deleted or disabled")
        return FakeExecutor()

    def _context(self) -> AppContext:
        async def factory(_sid: str, _record: Any) -> FakeExecutor:
            return FakeExecutor()

        telemetry = TelemetryService(self.settings, self.registry, factory)
        distribution = DistributionService(
            self.settings,
            self.workspace,
            executor_for=self._executor_for,
            active_transfers=lambda: [],
        )
        return AppContext(
            self.settings,
            ssh=FakeSsh(),  # type: ignore[arg-type]
            telemetry=telemetry,
            workspace=self.workspace,
            distribution=distribution,
        )


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[tuple[TestClient, WorkspaceRepository]]:
    harness = Harness(tmp_path, servers=["srv-a"])
    with harness.client as client:
        yield client, harness.workspace_repo


def test_project_crud_api(tmp_path: Path, harness: tuple[TestClient, WorkspaceRepository]) -> None:
    client, repo = harness
    artifact = client.post(
        "/api/v1/workspace/artifacts",
        json={"kind": "dataset", "name": "IVMSD", "version": "v1"},
    )
    assert artifact.status_code == 201
    artifact_id = artifact.json()["artifact_id"]

    created = client.post(
        "/api/v1/workspace/projects",
        json={"name": "CMOS", "artifact_ids": [artifact_id]},
    )
    assert created.status_code == 201
    project_id = created.json()["project_id"]

    listed = client.get("/api/v1/workspace/projects").json()
    assert [p["name"] for p in listed] == ["CMOS"]

    patched = client.patch(
        f"/api/v1/workspace/projects/{project_id}", json={"description": "updated"}
    )
    assert patched.status_code == 200 and patched.json()["description"] == "updated"

    deleted = client.delete(f"/api/v1/workspace/projects/{project_id}")
    assert deleted.status_code == 204
    assert repo.projects() == []

    unknown = client.patch("/api/v1/workspace/projects/ghost", json={"description": "x"})
    assert unknown.status_code == 404
    extra = client.post("/api/v1/workspace/projects", json={"name": "X", "shell": "rm -rf /"})
    assert extra.status_code == 422


def test_artifact_api_and_referential_rules(tmp_path: Path) -> None:
    with Harness(tmp_path, servers=["srv-a"]).client as client:
        _artifact_api_rules(client)


def _artifact_api_rules(client: TestClient) -> None:
    created = client.post("/api/v1/workspace/artifacts", json={"kind": "dataset", "name": "IVMSD"})
    assert created.status_code == 201
    artifact_id = created.json()["artifact_id"]
    assert created.json()["immutable"] is True

    patched = client.patch(f"/api/v1/workspace/artifacts/{artifact_id}", json={"description": "d"})
    assert patched.status_code == 200

    project = client.post("/api/v1/workspace/projects", json={"name": "P"})
    link = client.patch(
        f"/api/v1/workspace/projects/{project.json()['project_id']}",
        json={"artifact_ids": [artifact_id]},
    )
    assert link.status_code == 200
    blocked = client.delete(f"/api/v1/workspace/artifacts/{artifact_id}")
    assert blocked.status_code == 409


def test_placement_crud_and_inspect(tmp_path: Path) -> None:
    with Harness(tmp_path, servers=["srv-a", "srv-b"]).client as client:
        _placement_crud_and_inspect(client)


def _placement_crud_and_inspect(client: TestClient) -> None:
    artifact = client.post(
        "/api/v1/workspace/artifacts", json={"kind": "dataset", "name": "IVMSD"}
    ).json()
    placement = client.post(
        "/api/v1/workspace/placements",
        json={
            "artifact_id": artifact["artifact_id"],
            "server_id": "srv-a",
            "remote_path": "/data/IVMSD",
        },
    )
    assert placement.status_code == 201
    placement_id = placement.json()["placement_id"]

    moved = client.patch(
        f"/api/v1/workspace/placements/{placement_id}",
        json={"remote_path": "/data2/IVMSD"},
    )
    assert moved.status_code == 200 and moved.json()["remote_path"] == "/data2/IVMSD"

    inspect = client.post(f"/api/v1/workspace/placements/{placement_id}/inspect")
    assert inspect.status_code == 200
    body = inspect.json()
    # offline fake returns empty output: classified as unavailable, never a crash
    assert body["placement_id"] == placement_id
    assert body["state"] in ("verified", "missing", "unavailable")

    removed = client.delete(f"/api/v1/workspace/placements/{placement_id}")
    assert removed.status_code == 204
    inspect_again = client.post(f"/api/v1/workspace/placements/{placement_id}/inspect")
    assert inspect_again.status_code == 404


def test_inspect_offline_server_maps_error(tmp_path: Path) -> None:
    with Harness(tmp_path, servers=["srv-a"]).client as client:
        _inspect_offline(client)


def _inspect_offline(client: TestClient) -> None:
    artifact = client.post("/api/v1/workspace/artifacts", json={"kind": "code", "name": "C"}).json()
    placement = client.post(
        "/api/v1/workspace/placements",
        json={
            "artifact_id": artifact["artifact_id"],
            "server_id": "srv-a",
            "remote_path": "~/code",
        },
    ).json()
    inspect = client.post(f"/api/v1/workspace/placements/{placement['placement_id']}/inspect")
    # FakeSsh returns empty stdout — inspection degrades to a typed state
    assert inspect.status_code == 200
    assert inspect.json()["state"] in ("missing", "unavailable", "verified")


def test_invalid_workspace_inputs_are_4xx(tmp_path: Path) -> None:
    with Harness(tmp_path, servers=["srv-a"]).client as client:
        _invalid_inputs(client)


def _invalid_inputs(client: TestClient) -> None:
    assert (
        client.post("/api/v1/workspace/projects", json={"name": "", "description": "x"}).status_code
        == 422
    )
    assert client.post("/api/v1/workspace/projects", json={"name": "x" * 200}).status_code == 422
    assert (
        client.post(
            "/api/v1/workspace/artifacts", json={"kind": "ghost-kind", "name": "X"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/workspace/launch-configs",
            json={"project_id": "ghost", "name": "n", "program": "p"},
        ).status_code
        == 404
    )


# ---- helpers kept at the end -----------------------------------------------------


def test_workspace_store_corrupt_tolerated(tmp_path: Path) -> None:
    store_path = tmp_path / "workspace.json"
    store_path.write_text("{corrupt json")
    repo = WorkspaceRepository(JsonFileStore(store_path))
    assert repo.projects() == []  # corrupt file never renamed at startup


def test_project_transfer_excludes_validation_and_resolution(tmp_path: Path) -> None:
    service, _repo = make_service(tmp_path)
    project = service.create_project(
        ProjectCreate(name="P", transfer_excludes=["dataset", "checkpoints"])
    )
    assert project.transfer_excludes == ["dataset", "checkpoints"]

    # Duplicates are collapsed; empty/blank entries are rejected.
    with pytest.raises(pydantic.ValidationError):
        service.update_project(project.project_id, ProjectPatch(transfer_excludes=["", "dataset"]))
    updated = service.update_project(
        project.project_id, ProjectPatch(transfer_excludes=["dataset", "dataset", "*.pth"])
    )
    assert updated.transfer_excludes == ["dataset", "*.pth"]
    assert service.project(project.project_id).transfer_excludes == ["dataset", "*.pth"]

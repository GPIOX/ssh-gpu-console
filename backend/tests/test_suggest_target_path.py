"""Manual transfer target-path suggestion (Part D): canonical safe naming.

The suggestion must reuse exactly the Project Sync helpers
(``safe_artifact_leaf`` + ``claim_path``), escalate against placements of
OTHER artifacts on the SAME server, and report missing roots by reason
instead of guessing a path. Self-contained fakes; no real network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.workspace import ArtifactCreate, PlacementCreate, ServerRootsUpdate
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from app.telemetry.service import TelemetryService
from app.workspace.distribution import DistributionService, ServerUnavailableError
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from fastapi.testclient import TestClient


class Scenario:
    """WorkspaceService + repository over a temp JSON store (test_sync_plan idiom)."""

    def __init__(self, tmp_path: Path) -> None:
        repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
        self.workspace = WorkspaceService(repo, server_exists=lambda _sid: True)

    def add_artifact(self, kind: str, name: str, version: str | None = None) -> str:
        artifact = self.workspace.create_artifact(
            ArtifactCreate(kind=kind, name=name, version=version)
        )
        return artifact.artifact_id

    def add_placement(self, artifact_id: str, server_id: str, remote_path: str) -> str:
        placement = self.workspace.create_placement(
            PlacementCreate(artifact_id=artifact_id, server_id=server_id, remote_path=remote_path)
        )
        return placement.placement_id


def make_scenario(tmp_path: Path, roots: ServerRootsUpdate | None = None) -> Scenario:
    sc = Scenario(tmp_path)
    if roots is not None:
        sc.workspace.set_server_roots("srv-a", roots)
    return sc


# ---- safe naming ------------------------------------------------------------------


def test_suggestion_uses_canonical_leaf_naming(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    artifact_id = sc.add_artifact("dataset", "MOCKSET", "v1")
    # safe_artifact_leaf semantics: version joins as name--version, NOT colon.
    assert sc.workspace.suggest_target_path("srv-a", artifact_id) == (
        "/data/dset/MOCKSET--v1",
        None,
    )


def test_suggestion_sanitizes_slashes_and_keeps_unicode_and_case(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    slashed = sc.add_artifact("dataset", "a/b", "v1")
    unicode_case = sc.add_artifact("dataset", "École模型")
    path, reason = sc.workspace.suggest_target_path("srv-a", slashed)
    assert (path, reason) == ("/data/dset/a-b--v1", None)
    path, reason = sc.workspace.suggest_target_path("srv-a", unicode_case)
    assert (path, reason) == ("/data/dset/École模型", None)


def test_suggestion_rejects_unusable_name_with_reason(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    dots = sc.add_artifact("dataset", "..")
    assert sc.workspace.suggest_target_path("srv-a", dots) == (None, "artifact_name_unusable")


# ---- collision escalation -------------------------------------------------------------


def test_suggestion_escalates_only_on_same_server_collision(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    artifact_a = sc.add_artifact("dataset", "MOCKSET", "v1")
    artifact_b = sc.add_artifact("dataset", "IVMSD")
    base_a = "/data/dset/MOCKSET--v1"
    # B occupies exactly A's base path on THIS server -> A must escalate.
    sc.add_placement(artifact_b, "srv-a", base_a)
    path, reason = sc.workspace.suggest_target_path("srv-a", artifact_a)
    assert reason is None
    assert path == f"{base_a}--{artifact_a[:6]}"
    # Deterministic: repeated calls return the identical path.
    assert sc.workspace.suggest_target_path("srv-a", artifact_a) == (path, None)


def test_suggestion_ignores_placements_on_other_servers(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    artifact_a = sc.add_artifact("dataset", "MOCKSET", "v1")
    artifact_b = sc.add_artifact("dataset", "IVMSD")
    # B sits on A's base path, but on a DIFFERENT server: no escalation.
    sc.add_placement(artifact_b, "srv-b", "/data/dset/MOCKSET--v1")
    assert sc.workspace.suggest_target_path("srv-a", artifact_a) == (
        "/data/dset/MOCKSET--v1",
        None,
    )
    # A's own placement (same artifact id) never collides with itself either.
    sc.add_placement(artifact_a, "srv-a", "/data/dset/MOCKSET--v1")
    assert sc.workspace.suggest_target_path("srv-a", artifact_a) == (
        "/data/dset/MOCKSET--v1",
        None,
    )


# ---- per-kind roots -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "field", "root"),
    [
        ("code", "project_root", "/proj"),
        ("dataset", "dataset_root", "/datasets"),
        ("model", "model_root", "/models"),
    ],
)
def test_suggestion_uses_each_kind_root(tmp_path: Path, kind: str, field: str, root: str) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(**{field: root}))
    artifact_id = sc.add_artifact(kind, "Net")
    assert sc.workspace.suggest_target_path("srv-a", artifact_id) == (f"{root}/Net", None)


# ---- missing roots ---------------------------------------------------------------------


def test_suggestion_missing_kind_root_reports_reason(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(project_root="/proj"))
    dataset = sc.add_artifact("dataset", "MOCKSET")
    model = sc.add_artifact("model", "Net")
    assert sc.workspace.suggest_target_path("srv-a", dataset) == (
        None,
        "server_root_not_configured",
    )
    assert sc.workspace.suggest_target_path("srv-a", model) == (
        None,
        "server_root_not_configured",
    )


def test_suggestion_missing_roots_record_reports_reason(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "MOCKSET")
    assert sc.workspace.suggest_target_path("srv-a", artifact_id) == (
        None,
        "server_root_not_configured",
    )


def test_suggestion_unknown_artifact_raises_not_found(tmp_path: Path) -> None:
    sc = make_scenario(tmp_path, ServerRootsUpdate(dataset_root="/data/dset"))
    with pytest.raises(NotFoundError):
        sc.workspace.suggest_target_path("srv-a", "missing-artifact")


# ---- route wiring -----------------------------------------------------------------------


class FakeExecutor:
    async def run(self, _command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        _ = timeout_s
        return RemoteCommandResult(0, "", "", 1.0)

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


@pytest.fixture()
def route_env(tmp_path: Path) -> Iterator[tuple[TestClient, WorkspaceService]]:
    settings = Settings(data_dir=tmp_path / "data")
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    from app.models.server import ServerCreate

    registry.create(ServerCreate(display_name="srv-a", ssh_host="srv-a.example"))
    set_default_registry(registry)

    async def factory(_sid: str, _record: Any) -> FakeExecutor:
        return FakeExecutor()

    def executor_for(server_id: str) -> FakeExecutor:
        if server_id not in {server.display_name for server in registry.all()}:
            raise ServerUnavailableError("server is unknown, deleted or disabled")
        return FakeExecutor()

    repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
    names = {server.display_name for server in registry.all()}
    workspace = WorkspaceService(repo, server_exists=lambda sid: sid in names)
    distribution = DistributionService(
        settings,
        workspace,
        executor_for=executor_for,
        active_transfers=lambda: [],
    )
    app_context = AppContext(
        settings,
        ssh=FakeSsh(),  # type: ignore[arg-type]
        telemetry=TelemetryService(settings, registry, factory),
        workspace=workspace,
        distribution=distribution,
    )
    client = TestClient(create_app(settings, context=app_context))
    with client:
        yield client, workspace


def test_route_suggests_canonical_path(route_env: tuple[TestClient, WorkspaceService]) -> None:
    client, workspace = route_env
    workspace.set_server_roots("srv-a", ServerRootsUpdate(dataset_root="/data/dset"))
    artifact = workspace.create_artifact(ArtifactCreate(kind="dataset", name="MOCKSET", version="v1"))

    response = client.get(
        f"/api/v1/workspace/artifacts/{artifact.artifact_id}/suggest-target-path",
        params={"server_id": "srv-a"},
    )
    assert response.status_code == 200
    assert response.json() == {"target_path": "/data/dset/MOCKSET--v1", "reason": None}


def test_route_unknown_artifact_is_404(route_env: tuple[TestClient, WorkspaceService]) -> None:
    client, _workspace = route_env
    response = client.get(
        "/api/v1/workspace/artifacts/missing-artifact/suggest-target-path",
        params={"server_id": "srv-a"},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_route_missing_root_reports_reason(route_env: tuple[TestClient, WorkspaceService]) -> None:
    client, workspace = route_env
    artifact = workspace.create_artifact(ArtifactCreate(kind="dataset", name="MOCKSET"))

    response = client.get(
        f"/api/v1/workspace/artifacts/{artifact.artifact_id}/suggest-target-path",
        params={"server_id": "srv-a"},
    )
    assert response.status_code == 200
    assert response.json() == {"target_path": None, "reason": "server_root_not_configured"}

"""DistributionService tests (Phase 4A.3 + 4B): read model, explicit checks,
bounded concurrency, cache sharing, zero-SSH guarantee. Self-contained fakes;
no real network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.distribution import DistributionState
from app.models.transfer import TransferJob, TransferState, TransferStrategy
from app.models.workspace import ArtifactCreate, InspectionState, PlacementCreate, ProjectCreate
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from app.telemetry.service import TelemetryService
from app.workspace.distribution import DistributionService, ServerUnavailableError
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from fastapi.testclient import TestClient

_EXISTS_STDOUT = "EXISTS directory|4096\n COUNT 3\n"  # matches the inspection command format
_MISSING_STDOUT = "MISSING\n"


# ---- fakes -----------------------------------------------------------------------


class ConcurrencyTracker:
    """Counts concurrently running fake SSH commands."""

    def __init__(self) -> None:
        self.current = 0
        self.max_seen = 0

    def enter(self) -> None:
        self.current += 1
        self.max_seen = max(self.max_seen, self.current)

    def exit(self) -> None:
        self.current -= 1


class ScriptedExecutor:
    """Fake executor: scripted stdout or a raised transport-style error."""

    def __init__(
        self,
        stdout: str = "",
        exc: Exception | None = None,
        tracker: ConcurrencyTracker | None = None,
    ) -> None:
        self.stdout = stdout
        self.exc = exc
        self.tracker = tracker

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        _ = command
        if self.tracker is not None:
            self.tracker.enter()
            try:
                await asyncio.sleep(0.05)
            finally:
                self.tracker.exit()
        if self.exc is not None:
            raise self.exc
        return RemoteCommandResult(0, self.stdout, "", 1.0)

    async def close(self) -> None:
        return None


class ExecutorFactoryFake:
    """`executor_for` double: records every request; scripted executors per
    server; raises for unknown servers (as the production factory does)."""

    def __init__(self) -> None:
        self.script: dict[str, ScriptedExecutor] = {}
        self.unknown: set[str] = set()
        self.requested: list[str] = []

    def __call__(self, server_id: str) -> ScriptedExecutor:
        self.requested.append(server_id)
        if server_id in self.unknown:
            raise ServerUnavailableError("server is unknown, deleted or disabled")
        return self.script[server_id]


def _job(job_id: str, artifact_id: str, target_server_id: str, state: TransferState) -> TransferJob:
    return TransferJob(
        job_id=job_id,
        artifact_id=artifact_id,
        source_server_id="srv-src",
        source_path="/src",
        target_server_id=target_server_id,
        target_path="/dst",
        strategy_requested=TransferStrategy.AUTO,
        state=state,
    )


class Scenario:
    """Workspace declarations + DistributionService over fake executors."""

    def __init__(self, tmp_path: Path, settings: Settings | None = None) -> None:
        self.settings = settings or Settings(data_dir=tmp_path / "data")
        repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
        self.workspace = WorkspaceService(repo, server_exists=lambda _sid: True)
        self.factory = ExecutorFactoryFake()
        self.active_jobs: list[TransferJob] = []
        self.service = DistributionService(
            self.settings,
            self.workspace,
            executor_for=self.factory,
            active_transfers=lambda: self.active_jobs,
        )

    def add_placement(
        self, artifact_name: str, server_id: str, remote_path: str
    ) -> tuple[str, str]:
        """Create artifact + placement; returns (artifact_id, placement_id)."""
        artifact = self.workspace.create_artifact(
            ArtifactCreate(kind="dataset", name=artifact_name)
        )
        placement = self.workspace.create_placement(
            PlacementCreate(
                artifact_id=artifact.artifact_id, server_id=server_id, remote_path=remote_path
            )
        )
        return artifact.artifact_id, placement.placement_id

    def add_project(self, name: str, artifact_ids: list[str]) -> str:
        project = self.workspace.create_project(ProjectCreate(name=name, artifact_ids=artifact_ids))
        return project.project_id


# ---- 1. declared -----------------------------------------------------------------


def test_declared_until_inspected(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id, placement_id = sc.add_placement("IVMSD", "srv-a", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])

    dist = sc.service.distribution(project_id)
    assert dist.project_id == project_id
    assert dist.generated_at != ""
    assert len(dist.items) == 1
    item = dist.items[0]
    assert item.placement_id == placement_id
    assert item.artifact_id == artifact_id
    assert item.artifact_label == "IVMSD"
    assert item.artifact_kind == "dataset"
    assert item.server_id == "srv-a"
    assert item.remote_path == "/data/IVMSD"
    assert item.state is DistributionState.DECLARED
    assert item.checked_at is None
    assert item.detail is None
    assert item.active_transfer_job_id is None


# ---- 2. verified / missing / unavailable ------------------------------------------


async def test_inspect_states_verified_missing_unavailable(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_ids: list[str] = []
    artifact_id, p_ok = sc.add_placement("IVMSD", "srv-ok", "/data/IVMSD")
    artifact_ids.append(artifact_id)
    artifact_id, p_missing = sc.add_placement("IVMSD", "srv-missing", "/data/GONE")
    artifact_ids.append(artifact_id)
    artifact_id, p_boom = sc.add_placement("IVMSD", "srv-boom", "/data/BOOM")
    artifact_ids.append(artifact_id)
    artifact_id, p_ghost = sc.add_placement("IVMSD", "srv-ghost", "/data/GHOST")
    artifact_ids.append(artifact_id)
    project_id = sc.add_project("CMOS", artifact_ids)
    sc.factory.script = {
        "srv-ok": ScriptedExecutor(_EXISTS_STDOUT),
        "srv-missing": ScriptedExecutor(_MISSING_STDOUT),
        "srv-boom": ScriptedExecutor(exc=RuntimeError("ssh transport exploded")),
    }
    sc.factory.unknown = {"srv-ghost"}

    dist = await sc.service.inspect_project(project_id)
    by_placement = {item.placement_id: item for item in dist.items}
    verified = by_placement[p_ok]
    assert verified.state is DistributionState.VERIFIED
    assert verified.checked_at is not None
    assert verified.detail is None
    assert by_placement[p_missing].state is DistributionState.MISSING
    assert by_placement[p_missing].checked_at is not None
    unavailable = by_placement[p_boom]
    assert unavailable.state is DistributionState.UNAVAILABLE
    assert "exploded" in (unavailable.detail or "")
    assert unavailable.checked_at is not None
    ghost = by_placement[p_ghost]
    assert ghost.state is DistributionState.UNAVAILABLE
    assert "unknown" in (ghost.detail or "")


# ---- 3. syncing overlay ------------------------------------------------------------


async def test_syncing_overlay_and_terminal_jobs(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id, placement_id = sc.add_placement("IVMSD", "srv-a", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])

    sc.active_jobs.append(_job("job-1", artifact_id, "srv-a", TransferState.RUNNING))
    dist = sc.service.distribution(project_id)
    assert dist.items[0].state is DistributionState.SYNCING
    assert dist.items[0].active_transfer_job_id == "job-1"

    # Even a fresh VERIFIED observation is overridden by the active job.
    sc.factory.script["srv-a"] = ScriptedExecutor(_EXISTS_STDOUT)
    await sc.service.inspect_placement(placement_id)
    dist = sc.service.distribution(project_id)
    assert dist.items[0].state is DistributionState.SYNCING
    assert dist.items[0].active_transfer_job_id == "job-1"

    # Terminal jobs produce no overlay.
    sc.active_jobs = [_job("job-1", artifact_id, "srv-a", TransferState.COMPLETED)]
    dist = sc.service.distribution(project_id)
    assert dist.items[0].state is DistributionState.VERIFIED
    assert dist.items[0].active_transfer_job_id is None


# ---- 4. zero-SSH read model ---------------------------------------------------------


async def test_distribution_is_zero_ssh(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id, _placement_id = sc.add_placement("IVMSD", "srv-a", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])

    sc.service.distribution(project_id)
    assert sc.factory.requested == []  # reading the matrix never builds an executor

    await sc.service.inspect_project(project_id)
    assert sc.factory.requested == ["srv-a"]  # explicit inspection does touch SSH


# ---- 5. bounded concurrency ----------------------------------------------------------


async def test_inspection_concurrency_is_bounded(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", project_inspection_concurrency=2)
    sc = Scenario(tmp_path, settings=settings)
    tracker = ConcurrencyTracker()
    artifact_ids: list[str] = []
    for n in range(8):
        artifact_id, _placement_id = sc.add_placement(f"A{n}", f"srv-{n}", f"/data/A{n}")
        artifact_ids.append(artifact_id)
        sc.factory.script[f"srv-{n}"] = ScriptedExecutor(_MISSING_STDOUT, tracker=tracker)
    project_id = sc.add_project("CMOS", artifact_ids)

    dist = await sc.service.inspect_project(project_id)
    assert len(dist.items) == 8
    assert len(sc.factory.requested) == 8
    assert tracker.max_seen == 2  # parallel, but never beyond the configured bound
    assert tracker.max_seen <= 2


# ---- 6. shared observation cache ------------------------------------------------------


async def test_project_and_placement_checks_share_cache(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id, placement_id = sc.add_placement("IVMSD", "srv-ok", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-ok"] = ScriptedExecutor(_EXISTS_STDOUT)

    # project check first, then the single-placement check reads the same cache
    dist = await sc.service.inspect_project(project_id)
    single = await sc.service.inspect_placement(placement_id)
    assert single.state is InspectionState.VERIFIED
    assert single.checked_at == dist.items[0].checked_at

    # reverse order: single check, then the project read model sees it
    sc2 = Scenario(tmp_path / "reverse")
    artifact_id2, placement_id2 = sc2.add_placement("IVMSD", "srv-ok", "/data/IVMSD")
    project_id2 = sc2.add_project("CMOS", [artifact_id2])
    sc2.factory.script["srv-ok"] = ScriptedExecutor(_EXISTS_STDOUT)
    single2 = await sc2.service.inspect_placement(placement_id2)
    dist2 = sc2.service.distribution(project_id2)
    assert dist2.items[0].state is DistributionState.VERIFIED
    assert dist2.items[0].checked_at == single2.checked_at


# ---- 7. restart resets observations ----------------------------------------------------


async def test_restart_resets_to_declared(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id, _placement_id = sc.add_placement("IVMSD", "srv-ok", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-ok"] = ScriptedExecutor(_EXISTS_STDOUT)
    await sc.service.inspect_project(project_id)
    assert sc.service.distribution(project_id).items[0].state is DistributionState.VERIFIED

    # New process == new service with an empty observation cache.
    restarted = DistributionService(
        sc.settings,
        sc.workspace,
        executor_for=sc.factory,
        active_transfers=lambda: [],
    )
    dist = restarted.distribution(project_id)
    assert all(item.state is DistributionState.DECLARED for item in dist.items)


# ---- 8. unknown project -----------------------------------------------------------------


async def test_unknown_project_raises_not_found(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    with pytest.raises(NotFoundError):
        sc.service.distribution("ghost")
    with pytest.raises(NotFoundError):
        await sc.service.inspect_project("ghost")


# ---- 9. API smoke ------------------------------------------------------------------------


class FakeSsh:
    async def run(
        self, _server: Any, _command: str, *, timeout_s: float | None = None
    ) -> RemoteCommandResult:
        _ = timeout_s
        return RemoteCommandResult(0, "", "", 1.0)

    async def close_server(self, _server_id: str) -> None:
        return None

    async def close_all(self) -> None:
        return None


class ApiExecutorFactory:
    """executor_for double for the API harness: records requests, raises for
    servers unknown to the registry, returns a scripted fake otherwise."""

    def __init__(self, registry: ServerRegistry) -> None:
        self._registry = registry
        self.requested: list[str] = []

    def __call__(self, server_id: str) -> ScriptedExecutor:
        self.requested.append(server_id)
        try:
            self._registry.get(server_id)
        except Exception as exc:
            raise ServerUnavailableError("server is unknown, deleted or disabled") from exc
        return ScriptedExecutor(_EXISTS_STDOUT)


class Harness:
    """API harness mirroring test_workspace.Harness, with distribution wired."""

    def __init__(self, tmp_path: Path, servers: list[str] | None = None) -> None:
        servers = servers if servers is not None else ["srv-a"]
        self.settings = Settings(
            data_dir=tmp_path / "data",
            fleet_interval_active=1.0,
            fleet_interval_idle=2.0,
        )
        self.registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
        from app.models.server import ServerCreate

        for server_id in servers:
            self.registry.create(
                ServerCreate(display_name=server_id, ssh_host=f"{server_id}.example")
            )
        set_default_registry(self.registry)
        self.workspace_repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
        self.workspace = WorkspaceService(
            self.workspace_repo,
            server_exists=lambda sid: sid in self._names() or sid in self._ids(),
        )
        self.factory = ApiExecutorFactory(self.registry)
        telemetry = TelemetryService(self.settings, self.registry, self._telemetry_executor)
        distribution = DistributionService(
            self.settings,
            self.workspace,
            executor_for=self.factory,
            active_transfers=lambda: [],
        )
        context = AppContext(
            self.settings,
            ssh=FakeSsh(),  # type: ignore[arg-type]
            telemetry=telemetry,
            workspace=self.workspace,
            distribution=distribution,
        )
        self.client = TestClient(create_app(self.settings, context=context))

    def _names(self) -> set[str]:
        return {server.display_name for server in self.registry.all()}

    def _ids(self) -> set[str]:
        return {server.server_id for server in self.registry.all()}

    async def _telemetry_executor(self, _sid: str, _record: Any) -> ScriptedExecutor:
        return ScriptedExecutor()


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    with harness.client as client:
        _ = client
        yield harness


def test_api_distribution_endpoints(harness: Harness) -> None:
    client = harness.client
    server_id = harness.registry.all()[0].server_id  # real generated registry id
    artifact = client.post(
        "/api/v1/workspace/artifacts", json={"kind": "dataset", "name": "IVMSD"}
    ).json()
    project = client.post(
        "/api/v1/workspace/projects",
        json={"name": "CMOS", "artifact_ids": [artifact["artifact_id"]]},
    ).json()
    placement = client.post(
        "/api/v1/workspace/placements",
        json={
            "artifact_id": artifact["artifact_id"],
            "server_id": server_id,
            "remote_path": "/data/IVMSD",
        },
    )
    assert placement.status_code == 201
    project_id = project["project_id"]

    # GET: zero-SSH read model, declared until inspected
    got = client.get(f"/api/v1/workspace/projects/{project_id}/distribution")
    assert got.status_code == 200
    body = got.json()
    assert body["project_id"] == project_id
    assert body["items"][0]["state"] == "declared"
    assert harness.factory.requested == []

    # POST inspect: runs the checks, returns the fresh read model
    inspected = client.post(f"/api/v1/workspace/projects/{project_id}/inspect")
    assert inspected.status_code == 200
    assert inspected.json()["items"][0]["state"] == "verified"
    assert harness.factory.requested == [server_id]

    # Subsequent GETs stay zero-SSH and show the cached observation
    again = client.get(f"/api/v1/workspace/projects/{project_id}/distribution").json()
    assert again["items"][0]["state"] == "verified"
    assert harness.factory.requested == [server_id]

    # Placement inspect goes through the same service + cache
    single = client.post(f"/api/v1/workspace/placements/{placement.json()['placement_id']}/inspect")
    assert single.status_code == 200
    assert single.json()["state"] == "verified"

    # Unknown project -> typed 404
    assert client.get("/api/v1/workspace/projects/ghost/distribution").status_code == 404
    assert client.post("/api/v1/workspace/projects/ghost/inspect").status_code == 404


def test_api_inspect_unknown_server_is_unavailable(harness: Harness) -> None:
    # "srv-a" is only a display name, not a registry id: creation passes
    # (server_exists accepts it), inspection must degrade to UNAVAILABLE.
    client = harness.client
    artifact = client.post("/api/v1/workspace/artifacts", json={"kind": "code", "name": "C"}).json()
    placement = client.post(
        "/api/v1/workspace/placements",
        json={
            "artifact_id": artifact["artifact_id"],
            "server_id": "srv-a",
            "remote_path": "~/code",
        },
    )
    assert placement.status_code == 201
    inspect = client.post(
        f"/api/v1/workspace/placements/{placement.json()['placement_id']}/inspect"
    )
    assert inspect.status_code == 200
    body = inspect.json()
    assert body["state"] == "unavailable"
    assert "unknown" in body["detail"]

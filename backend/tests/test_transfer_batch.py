"""Phase 4D TransferBatch tests: batch orchestration over the REAL transfer
service, state derivation, plan regeneration at the API boundary, RAM-only
forgetting on restart, and the no-rollback guarantee.

Self-contained fakes; no real network. Service-level tests reuse the relay
harness from test_transfer_service_resume (the relay is monkeypatched AT the
strategy boundary, so no transport module is ever imported).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import NotFoundError
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.distribution import (
    ArtifactSyncItem,
    DistributionState,
    ProjectSyncPlan,
    SyncAction,
    SyncPlanRequest,
)
from app.models.server import ServerCreate, ServerPatch
from app.models.transfer import (
    TransferBatch,
    TransferBatchState,
    TransferJob,
    TransferRequest,
    TransferState,
    TransferStrategy,
)
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from app.telemetry.service import TelemetryService
from app.transfer.batch import BatchRegistry
from app.transfer.service import TransferService
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from fastapi.testclient import TestClient

from tests.test_transfer_service_resume import (
    FakeSsh,
    FakeTransferSession,
    MemFS,
    _install_relay,
    _wait_terminal,
)
from tests.test_transfer_service_resume import (
    Harness as ServiceHarness,
)

CreateJob = Callable[[ArtifactSyncItem], str]

_ALLOWED_PERSISTED_FILES = {"workspace.json", "registry.json"}


def _persisted_files(tmp_path: Path) -> set[str]:
    return {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}


# ---- plan/item factories ----------------------------------------------------------


def _plan(
    project_id: str,
    target_server_id: str,
    items: list[ArtifactSyncItem],
    *,
    valid: bool = True,
    error: str = "",
) -> ProjectSyncPlan:
    return ProjectSyncPlan(
        project_id=project_id,
        target_server_id=target_server_id,
        generated_at="2026-01-01T00:00:00+00:00",
        refresh_code=False,
        items=items,
        valid=valid,
        error=error,
    )


def _transfer_item(
    artifact_id: str, *, source: str = "plc-src", target: str = "/data/x"
) -> ArtifactSyncItem:
    return ArtifactSyncItem(
        artifact_id=artifact_id,
        artifact_label=f"art-{artifact_id}",
        artifact_kind="dataset",
        target_status=DistributionState.MISSING,
        action=SyncAction.TRANSFER,
        reason="no placement declared on the target server",
        source_placement_id=source,
        source_server_id="srv-src",
        source_path="/src/x",
        target_path=target,
        strategy_selected=TransferStrategy.LOCAL_RELAY,
    )


def _skip_item(artifact_id: str) -> ArtifactSyncItem:
    return ArtifactSyncItem(
        artifact_id=artifact_id,
        artifact_label=f"art-{artifact_id}",
        artifact_kind="dataset",
        target_status=DistributionState.VERIFIED,
        action=SyncAction.SKIP,
        reason="target placement is verified",
    )


def _unresolved_item(artifact_id: str) -> ArtifactSyncItem:
    return ArtifactSyncItem(
        artifact_id=artifact_id,
        artifact_label=f"art-{artifact_id}",
        artifact_kind="dataset",
        target_status=DistributionState.MISSING,
        action=SyncAction.UNRESOLVED,
        reason="no usable source placement: no other server declares this artifact",
    )


def _unexpected_create(_item: ArtifactSyncItem) -> str:
    raise AssertionError("empty plan must not create jobs")


# ---- API harness (fake planner + fake transfer service + real BatchRegistry) -------


class ApiSsh:
    """Duck SshManager for the API harness: nothing to run, nothing to close."""

    async def run(
        self, _server: Any, _command: str, *, timeout_s: float | None = None
    ) -> RemoteCommandResult:
        _ = timeout_s
        return RemoteCommandResult(0, "", "", 1.0)

    async def close_all(self) -> None:
        return None


class FakePlanner:
    """``build_plan`` double: returns a scripted plan and counts every call."""

    def __init__(self, plan: ProjectSyncPlan) -> None:
        self.plan = plan
        self.calls: list[tuple[str, SyncPlanRequest]] = []

    async def build_plan(self, project_id: str, request: SyncPlanRequest) -> ProjectSyncPlan:
        self.calls.append((project_id, request))
        return self.plan


class FakeTransfers:
    """Duck TransferService: records create() requests, serves job views."""

    def __init__(self) -> None:
        self.requests: list[TransferRequest] = []
        self.jobs: dict[str, TransferJob] = {}
        self._next = 0

    def create(self, request: TransferRequest) -> dict[str, str]:
        self.requests.append(request)
        self._next += 1
        job_id = f"job-{self._next}"
        self.jobs[job_id] = TransferJob(
            job_id=job_id,
            artifact_id=request.artifact_id,
            source_server_id="srv-src",
            source_path="/src/x",
            target_server_id=request.target_server_id,
            target_path=request.target_path,
            strategy_requested=request.strategy,
        )
        return {"job_id": job_id}

    def list_jobs(self) -> list[TransferJob]:
        return list(self.jobs.values())

    async def stop(self) -> None:
        return None


class ApiHarness:
    """API harness mirroring test_sync_plan's shape: everything through
    AppContext, with a real BatchRegistry and fake planner/transfers."""

    def __init__(
        self, tmp_path: Path, *, server_name: str = "srv-tgt", enabled: bool = True
    ) -> None:
        self.tmp_path = tmp_path
        self.settings = Settings(
            data_dir=tmp_path / "data",
            fleet_interval_active=1.0,
            fleet_interval_idle=2.0,
        )
        self.registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
        record = self.registry.create(
            ServerCreate(display_name=server_name, ssh_host=f"{server_name}.example")
        )
        self.server_id = record.server_id
        if not enabled:
            self.registry.update(record.server_id, ServerPatch(enabled=False))
        set_default_registry(self.registry)
        self.batches = BatchRegistry()
        self.planner = FakePlanner(_plan("p-1", self.server_id, []))
        self.transfers = FakeTransfers()
        telemetry = TelemetryService(self.settings, self.registry, self._telemetry_executor)
        context = AppContext(
            self.settings,
            ssh=ApiSsh(),  # type: ignore[arg-type]
            telemetry=telemetry,
            transfers=self.transfers,  # type: ignore[arg-type]
            batches=self.batches,
            sync_planner=self.planner,  # type: ignore[arg-type]
        )
        self.client = TestClient(create_app(self.settings, context=context))

    async def _telemetry_executor(self, _sid: str, _record: Any) -> Any:
        return ApiSsh()


@pytest.fixture()
def api(tmp_path: Path) -> Iterator[ApiHarness]:
    harness = ApiHarness(tmp_path)
    with harness.client as client:
        _ = client
        yield harness


def _post_sync(client: TestClient, server_id: str) -> Any:
    return client.post("/api/v1/workspace/projects/p-1/sync", json={"target_server_id": server_id})


# ---- 1. only TRANSFER items create jobs ---------------------------------------------


def test_only_transfer_items_create_jobs(api: ApiHarness) -> None:
    api.planner.plan = _plan("p-1", api.server_id, [_transfer_item("a-1"), _skip_item("a-2")])
    got = _post_sync(api.client, api.server_id)

    assert got.status_code == 200
    body = got.json()
    assert body["total_jobs"] == 1
    assert body["job_ids"] == ["job-1"]
    assert [request.artifact_id for request in api.transfers.requests] == ["a-1"]
    assert len(api.planner.calls) == 1  # exactly one fresh build_plan per POST


# ---- 2. all-SKIP plan -> legal empty batch -------------------------------------------


def test_all_skip_plan_yields_legal_empty_batch(api: ApiHarness) -> None:
    api.planner.plan = _plan("p-1", api.server_id, [_skip_item("a-1"), _skip_item("a-2")])
    got = _post_sync(api.client, api.server_id)

    assert got.status_code == 200
    body = got.json()
    assert body["state"] == TransferBatchState.COMPLETED.value
    assert body["total_jobs"] == 0
    assert body["job_ids"] == []
    assert api.transfers.requests == []  # nothing to transfer, nothing created


# ---- 3. UNRESOLVED / overlap / unknown+disabled target -> 409 ------------------------


def test_unresolved_items_refuse_sync_and_create_nothing(api: ApiHarness) -> None:
    unresolved = _unresolved_item("a-1")
    api.planner.plan = _plan("p-1", api.server_id, [unresolved, _transfer_item("a-2")])
    got = _post_sync(api.client, api.server_id)

    assert got.status_code == 409
    message = got.json()["message"]
    assert "art-a-1" in message and unresolved.reason in message
    assert api.transfers.requests == []  # not a single job was created
    assert api.client.get("/api/v1/transfer-batches").json() == []  # and no batch either


def test_invalid_plan_overlap_refuses_sync(api: ApiHarness) -> None:
    error = "target path overlap: art-a-1 (/data/x) vs art-a-2 (/data/x/sub)"
    api.planner.plan = _plan(
        "p-1",
        api.server_id,
        [_transfer_item("a-1", target="/data/x"), _transfer_item("a-2", target="/data/x/sub")],
        valid=False,
        error=error,
    )
    got = _post_sync(api.client, api.server_id)

    assert got.status_code == 409
    assert error in got.json()["message"]  # the planner's error, verbatim
    assert api.transfers.requests == []


def test_unknown_target_server_refuses_sync_before_planning(api: ApiHarness) -> None:
    got = _post_sync(api.client, "ghost-server")

    assert got.status_code == 409
    assert "unknown" in got.json()["message"]
    assert api.planner.calls == []  # refused before any planning SSH
    assert api.transfers.requests == []


def test_disabled_target_server_refuses_sync(tmp_path: Path) -> None:
    api = ApiHarness(tmp_path, server_name="srv-off", enabled=False)
    with api.client as client:
        got = client.post(
            "/api/v1/workspace/projects/p-1/sync",
            json={"target_server_id": api.server_id},
        )
        assert got.status_code == 409
        assert "disabled" in got.json()["message"]
        assert api.planner.calls == []
        assert api.transfers.requests == []


# ---- 4. batch view fields after creation ----------------------------------------------


def test_batch_view_fields_after_creation(api: ApiHarness) -> None:
    api.planner.plan = _plan(
        "p-1",
        api.server_id,
        [_transfer_item("a-1", target="/data/one"), _transfer_item("a-2", target="/data/two")],
    )
    got = _post_sync(api.client, api.server_id)

    assert got.status_code == 200
    body = got.json()
    assert body["batch_id"]
    assert body["project_id"] == "p-1"  # the URL's project, not the client's
    assert body["target_server_id"] == api.server_id
    assert body["job_ids"] == ["job-1", "job-2"]
    assert body["total_jobs"] == 2
    assert body["created_at"]
    assert body["state"] == TransferBatchState.QUEUED.value
    assert body["queued_jobs"] == 2
    assert body["running_jobs"] == 0
    assert body["completed_jobs"] == 0
    assert body["failed_jobs"] == 0
    assert body["cancelled_jobs"] == 0
    # the jobs carry exactly the plan's decisions
    first = api.transfers.requests[0]
    assert first.artifact_id == "a-1"
    assert first.source_placement_id == "plc-src"
    assert first.target_server_id == api.server_id
    assert first.target_path == "/data/one"
    assert first.strategy == TransferStrategy.LOCAL_RELAY


# ---- 5. state derivation matrix (registry level) ----------------------------------------


async def test_state_derivation_matrix() -> None:
    registry = BatchRegistry()
    items = [_transfer_item("a-1"), _transfer_item("a-2"), _transfer_item("a-3")]
    ids = iter(["j-1", "j-2", "j-3"])
    batch = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=items,
        create_job=lambda _item: next(ids),
    )
    assert batch.state is TransferBatchState.QUEUED
    assert batch.queued_jobs == 3

    def view(states: dict[str, TransferState]) -> TransferBatch:
        return registry.get(batch.batch_id, states)

    # planning/verifying count as running (still derivable: not terminal yet)
    planning = view(
        {
            "j-1": TransferState.PLANNING,
            "j-2": TransferState.QUEUED,
            "j-3": TransferState.VERIFYING,
        }
    )
    assert planning.state is TransferBatchState.RUNNING
    assert planning.running_jobs == 2 and planning.queued_jobs == 1

    running = view(
        {"j-1": TransferState.RUNNING, "j-2": TransferState.COMPLETED, "j-3": TransferState.QUEUED}
    )
    assert running.state is TransferBatchState.RUNNING

    done = view({job_id: TransferState.COMPLETED for job_id in ("j-1", "j-2", "j-3")})
    assert done.state is TransferBatchState.COMPLETED
    assert done.completed_jobs == 3

    # the first terminal read froze the final view: a later read where the job
    # ids are unknown (evicted history) no longer flips the batch to QUEUED
    evicted = view({})
    assert evicted == done
    assert evicted.state is TransferBatchState.COMPLETED
    assert evicted.completed_jobs == 3

    # terminal mixes below each get their own batch: a record freezes at its
    # FIRST terminal observation, so one batch cannot show two terminal states
    async def frozen_batch(states: list[TransferState]) -> TransferBatch:
        job_ids = [f"j-x{index}" for index in range(len(states))]
        pending = iter(job_ids)
        started = await registry.start(
            project_id="p-1",
            target_server_id="srv-tgt",
            transfer_items=[_transfer_item(f"a-x{index}") for index in range(len(states))],
            create_job=lambda _item: next(pending),
        )
        return registry.get(started.batch_id, dict(zip(job_ids, states, strict=True)))

    mixed = await frozen_batch(
        [TransferState.FAILED, TransferState.COMPLETED, TransferState.COMPLETED]
    )
    assert mixed.state is TransferBatchState.PARTIAL_FAILED
    assert mixed.failed_jobs == 1 and mixed.completed_jobs == 2

    all_failed = await frozen_batch([TransferState.FAILED] * 3)
    assert all_failed.state is TransferBatchState.FAILED

    failed_and_cancelled = await frozen_batch(
        [TransferState.FAILED, TransferState.CANCELLED, TransferState.CANCELLED]
    )
    assert failed_and_cancelled.state is TransferBatchState.FAILED

    all_cancelled = await frozen_batch([TransferState.CANCELLED] * 3)
    assert all_cancelled.state is TransferBatchState.CANCELLED

    cancelled_and_completed = await frozen_batch(
        [TransferState.CANCELLED, TransferState.COMPLETED, TransferState.COMPLETED]
    )
    assert cancelled_and_completed.state is TransferBatchState.PARTIAL_FAILED


# ---- 5b. terminal batches keep their frozen snapshot after job eviction -----------------


async def test_completed_batch_freezes_against_job_eviction() -> None:
    registry = BatchRegistry()
    ids = iter(["j-1"])
    batch = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=[_transfer_item("a-1")],
        create_job=lambda _item: next(ids),
    )
    done = registry.get(batch.batch_id, {"j-1": TransferState.COMPLETED})
    assert done.state is TransferBatchState.COMPLETED and done.completed_jobs == 1

    # the job later falls out of the transfer history (simulated eviction):
    # the batch keeps its final terminal snapshot instead of flipping back
    evicted = registry.get(batch.batch_id, {})
    assert evicted == done
    assert evicted.state is TransferBatchState.COMPLETED
    assert evicted.completed_jobs == 1 and evicted.queued_jobs == 0
    assert evicted.total_jobs == 1
    assert registry.list({}) == [evicted]  # listed (frozen) as well


async def test_partial_failed_batch_freezes_against_job_eviction() -> None:
    registry = BatchRegistry()
    ids = iter(["j-1", "j-2"])
    batch = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=[_transfer_item("a-1"), _transfer_item("a-2")],
        create_job=lambda _item: next(ids),
    )
    mixed = registry.get(
        batch.batch_id, {"j-1": TransferState.FAILED, "j-2": TransferState.COMPLETED}
    )
    assert mixed.state is TransferBatchState.PARTIAL_FAILED
    assert mixed.failed_jobs == 1 and mixed.completed_jobs == 1

    evicted = registry.get(batch.batch_id, {})
    assert evicted == mixed
    assert evicted.state is TransferBatchState.PARTIAL_FAILED
    assert evicted.failed_jobs == 1 and evicted.completed_jobs == 1


async def test_empty_batch_freezes_completed_from_first_view() -> None:
    registry = BatchRegistry()
    empty = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=[],
        create_job=_unexpected_create,
    )
    assert empty.state is TransferBatchState.COMPLETED  # terminal from the first view

    again = registry.get(empty.batch_id, {})
    assert again == empty  # repeated reads return an equal (frozen) view
    assert registry.list({}) == [again]


async def test_empty_batch_is_completed_and_listing_is_newest_first() -> None:
    registry = BatchRegistry()
    empty = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=[],
        create_job=_unexpected_create,
    )
    assert empty.state is TransferBatchState.COMPLETED
    assert empty.total_jobs == 0

    second = await registry.start(
        project_id="p-1",
        target_server_id="srv-tgt",
        transfer_items=[_transfer_item("a-1")],
        create_job=lambda _item: "j-1",
    )
    views = registry.list({})
    assert [view.batch_id for view in views] == [second.batch_id, empty.batch_id]


# ---- 6. target placement recorded exactly once -------------------------------------------


def _service_create(service: TransferService, target_server_id: str) -> CreateJob:
    """The API-layer create_job adapter, over the real TransferService."""

    def create_job(item: ArtifactSyncItem) -> str:
        return service.create(
            TransferRequest(
                artifact_id=item.artifact_id,
                source_placement_id=item.source_placement_id or "",
                target_server_id=target_server_id,
                target_path=item.target_path or "",
                strategy=item.strategy_selected or TransferStrategy.AUTO,
            )
        )["job_id"]

    return create_job


async def test_target_placement_recorded_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ServiceHarness(tmp_path)
    _install_relay(monkeypatch, [])
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/blob.bin", b"B" * 1024)
    artifact = harness.artifact("dataset", "DEDUP")
    source = harness.placement(artifact.artifact_id, "srv-a", "~/data/blob.bin")
    target_path = "~/out/blob.bin"
    harness.placement(artifact.artifact_id, "srv-b", target_path)  # already declared
    service = harness.new_service()

    batch = await BatchRegistry().start(
        project_id="p-1",
        target_server_id=harness.ids["srv-b"],
        transfer_items=[
            _transfer_item(artifact.artifact_id, source=source.placement_id, target=target_path)
        ],
        create_job=_service_create(service, harness.ids["srv-b"]),
    )
    job = await _wait_terminal(service, batch.job_ids[0])
    assert job.state is TransferState.COMPLETED

    triples = [(p.artifact_id, p.server_id, p.remote_path) for p in harness.workspace.placements()]
    assert triples.count((artifact.artifact_id, harness.ids["srv-b"], target_path)) == 1


# ---- 7. POST sync always regenerates the plan (never trusts the client) -------------------


def test_sync_always_regenerates_plan_and_is_idempotent(api: ApiHarness) -> None:
    api.planner.plan = _plan("p-1", api.server_id, [_skip_item("a-1")])

    # a client-supplied plan body is not even parseable (extra="forbid")
    stale = {
        "target_server_id": api.server_id,
        "items": [{"artifact_id": "a-1", "action": "transfer"}],
    }
    rejected = api.client.post("/api/v1/workspace/projects/p-1/sync", json=stale)
    assert rejected.status_code == 422

    first = _post_sync(api.client, api.server_id)
    second = _post_sync(api.client, api.server_id)
    assert first.status_code == 200 and second.status_code == 200
    assert len(api.planner.calls) == 2  # one fresh build_plan per executed POST
    assert all(project_id == "p-1" for project_id, _ in api.planner.calls)
    assert all(request.target_server_id == api.server_id for _, request in api.planner.calls)
    assert api.transfers.requests == []  # idempotent: the re-sync creates nothing
    batches = api.client.get("/api/v1/transfer-batches").json()
    assert len(batches) == 2  # each sync still leaves a (completed) empty batch
    assert all(batch["state"] == "completed" for batch in batches)


# ---- 8. RAM only: no new files beyond the existing persistence -----------------------------


def test_batches_are_ram_only(tmp_path: Path) -> None:
    api = ApiHarness(tmp_path)
    with api.client as client:
        api.planner.plan = _plan(
            "p-1", api.server_id, [_transfer_item("a-1"), _transfer_item("a-2")]
        )
        got = _post_sync(client, api.server_id)
        assert got.status_code == 200
        assert client.get("/api/v1/transfer-batches").status_code == 200
    assert _persisted_files(tmp_path) <= _ALLOWED_PERSISTED_FILES


# ---- 9. restart: batches forgotten, recorded placements survive -----------------------------


async def test_restart_drops_batches_but_keeps_placements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ServiceHarness(tmp_path)
    _install_relay(monkeypatch, [])
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/x.bin", b"x" * 512)
    artifact = harness.artifact("dataset", "RESTART")
    source = harness.placement(artifact.artifact_id, "srv-a", "~/data/x.bin")
    target_path = "~/out/x.bin"
    service = harness.new_service()
    registry = BatchRegistry()

    batch = await registry.start(
        project_id="p-1",
        target_server_id=harness.ids["srv-b"],
        transfer_items=[
            _transfer_item(artifact.artifact_id, source=source.placement_id, target=target_path)
        ],
        create_job=_service_create(service, harness.ids["srv-b"]),
    )
    job = await _wait_terminal(service, batch.job_ids[0])
    assert job.state is TransferState.COMPLETED
    states = {batch.job_ids[0]: TransferState.COMPLETED}
    assert registry.get(batch.batch_id, states).state is TransferBatchState.COMPLETED

    fresh = BatchRegistry()  # a restarted backend: RAM state is gone
    with pytest.raises(NotFoundError):
        fresh.get(batch.batch_id, {})
    assert fresh.list({}) == []

    # ...while the recorded placement survived in workspace.json
    reopened = WorkspaceService(
        WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json")),
        server_exists=lambda _sid: True,
    )
    triples = [(p.artifact_id, p.server_id, p.remote_path) for p in reopened.placements()]
    assert (artifact.artifact_id, harness.ids["srv-b"], target_path) in triples
    assert _persisted_files(tmp_path) <= _ALLOWED_PERSISTED_FILES


# ---- 10. no rollback: a failed job never touches its successful sibling ---------------------


class RecordingSession(FakeTransferSession):
    """Adds recording destructive ops (which the fake relay never calls)."""

    def __init__(self, fs: MemFS, calls: list[str]) -> None:
        super().__init__(fs)
        self._calls = calls

    async def remove(self, path: str) -> None:
        self._calls.append(f"remove {path}")

    async def rmdir(self, path: str) -> None:
        self._calls.append(f"rmdir {path}")

    async def rename(self, oldpath: str, newpath: str) -> None:
        self._calls.append(f"rename {oldpath} -> {newpath}")


class NoRemoveSsh(FakeSsh):
    """FakeSsh whose transfer sessions record every destructive call."""

    def __init__(self, fs_by_server: dict[str, MemFS]) -> None:
        super().__init__(fs_by_server)
        self.remove_calls: list[str] = []

    async def transfer_session(self, server: Any) -> RecordingSession:
        return RecordingSession(self._fs[str(server.server_id)], self.remove_calls)


async def test_failed_job_does_not_rollback_the_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ServiceHarness(tmp_path)
    harness.ssh = NoRemoveSsh(harness.fs_by_server)
    _install_relay(monkeypatch, [], fail_first=True)
    payloads = {"~/data/one.bin": b"ONE" * 100, "~/data/two.bin": b"TWO" * 100}
    for path, content in payloads.items():
        harness.fs_by_server[harness.ids["srv-a"]].add_file(path, content)
    one = harness.artifact("dataset", "ONE")
    two = harness.artifact("dataset", "TWO")
    p_one = harness.placement(one.artifact_id, "srv-a", "~/data/one.bin")
    p_two = harness.placement(two.artifact_id, "srv-a", "~/data/two.bin")
    service = harness.new_service()
    registry = BatchRegistry()

    batch = await registry.start(
        project_id="p-1",
        target_server_id=harness.ids["srv-b"],
        transfer_items=[
            _transfer_item(one.artifact_id, source=p_one.placement_id, target="~/out/one.bin"),
            _transfer_item(two.artifact_id, source=p_two.placement_id, target="~/out/two.bin"),
        ],
        create_job=_service_create(service, harness.ids["srv-b"]),
    )
    for job_id in batch.job_ids:
        await _wait_terminal(service, job_id)

    states = {job_id: service.job(job_id).state for job_id in batch.job_ids}
    assert sorted(state.value for state in states.values()) == ["completed", "failed"]
    batch_view = registry.get(batch.batch_id, states)
    assert batch_view.state is TransferBatchState.PARTIAL_FAILED
    assert batch_view.completed_jobs == 1 and batch_view.failed_jobs == 1

    assert harness.ssh.remove_calls == []  # no deletion, not even for the failed side

    completed = next(jid for jid, state in states.items() if state is TransferState.COMPLETED)
    job = service.job(completed)
    source_content = payloads[
        "~/data/one.bin" if job.artifact_id == one.artifact_id else "~/data/two.bin"
    ]
    assert harness.fs_by_server[harness.ids["srv-b"]].files[job.target_path] == source_content
    triples = [(p.artifact_id, p.server_id, p.remote_path) for p in harness.workspace.placements()]
    assert triples.count((job.artifact_id, harness.ids["srv-b"], job.target_path)) == 1


# ---- 11. API smoke: sync -> batch, listing, 404 --------------------------------------------


def test_api_sync_and_batch_listing_smoke(api: ApiHarness) -> None:
    api.planner.plan = _plan(
        "p-1",
        api.server_id,
        [
            _transfer_item("a-1", target="/data/one"),
            _transfer_item("a-2", target="/data/two"),
            _skip_item("a-3"),
        ],
    )
    got = _post_sync(api.client, api.server_id)
    assert got.status_code == 200
    batch_id = got.json()["batch_id"]

    detail = api.client.get(f"/api/v1/transfer-batches/{batch_id}")
    assert detail.status_code == 200
    assert detail.json()["total_jobs"] == 2

    # job states flow in from the transfer service: running jobs -> RUNNING batch
    for job in api.transfers.jobs.values():
        job.state = TransferState.RUNNING
    running = api.client.get(f"/api/v1/transfer-batches/{batch_id}").json()
    assert running["state"] == TransferBatchState.RUNNING.value
    assert running["running_jobs"] == 2 and running["queued_jobs"] == 0

    missing = api.client.get("/api/v1/transfer-batches/ghost")
    assert missing.status_code == 404

    # a second sync appends a batch; the listing is newest first
    second = _post_sync(api.client, api.server_id)
    assert second.status_code == 200
    ids = [batch["batch_id"] for batch in api.client.get("/api/v1/transfer-batches").json()]
    assert ids == [second.json()["batch_id"], batch_id]

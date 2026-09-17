"""Project Sync Plan tests (Phase 4C): per-artifact decisions, source
selection, safe target naming, overlap marking, reuse of the transfer
planner and the API wiring. Self-contained fakes; no real network.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.distribution import DistributionState, SyncAction, SyncPlanRequest
from app.models.transfer import (
    TransferJob,
    TransferPlan,
    TransferRequest,
    TransferState,
    TransferStrategy,
)
from app.models.workspace import (
    ArtifactCreate,
    PlacementCreate,
    ProjectCreate,
    ServerRootsUpdate,
)
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from app.telemetry.service import TelemetryService
from app.transfer.service import TransferService
from app.workspace.distribution import DistributionService, ServerUnavailableError
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService
from app.workspace.sync_planner import SyncPlanner
from fastapi.testclient import TestClient

_EXISTS_STDOUT = "EXISTS directory|4096\n COUNT 3\n"  # matches the inspection command format
_MISSING_STDOUT = "MISSING\n"


# ---- fakes -----------------------------------------------------------------------


def _command_path(command: str) -> str:
    """Both scripted command shapes start with the fixed prefix ``p=<path>;``."""
    return command[2 : command.index(";")]


class FakeExecutor:
    """Scripted executor for the planner's two command shapes.

    Source preflights (the ``-L "$p"`` probe) answer with ``preflight``
    ("OK" / "SYMLINK" / "MISSING"); placement inspections answer EXISTS unless
    the probed path is listed in ``missing_paths``. ``exc`` simulates a
    transport failure. Every command is recorded for assertions.
    """

    def __init__(
        self,
        preflight: str = "OK",
        missing_paths: set[str] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.preflight = preflight
        self.missing_paths = set(missing_paths) if missing_paths else set()
        self.exc = exc
        self.commands: list[str] = []

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        self.commands.append(command)
        _ = timeout_s
        if self.exc is not None:
            raise self.exc
        if '-L "$p"' in command:
            return RemoteCommandResult(0, self.preflight + "\n", "", 1.0)
        stdout = _MISSING_STDOUT if _command_path(command) in self.missing_paths else _EXISTS_STDOUT
        return RemoteCommandResult(0, stdout, "", 1.0)

    async def close(self) -> None:
        return None


class ExecutorFactoryFake:
    """`executor_for` double: records every request; scripted executors per
    server; raises for unknown servers (as the production factory does)."""

    def __init__(self) -> None:
        self.script: dict[str, FakeExecutor] = {}
        self.unknown: set[str] = set()
        self.requested: list[str] = []

    def __call__(self, server_id: str) -> FakeExecutor:
        self.requested.append(server_id)
        if server_id in self.unknown:
            raise ServerUnavailableError("server is unknown, deleted or disabled")
        return self.script[server_id]


class PlanRecorder:
    """`plan_transfer` double: counts requests and returns a fixed outcome."""

    def __init__(
        self,
        selected: TransferStrategy | None = TransferStrategy.LOCAL_RELAY,
        reason: str = "local relay requested",
    ) -> None:
        self.selected = selected
        self.reason = reason
        self.requests: list[TransferRequest] = []

    async def __call__(self, request: TransferRequest) -> TransferPlan:
        self.requests.append(request)
        return TransferPlan(
            artifact_id=request.artifact_id,
            source_server_id="srv-src",
            source_path="/src",
            target_server_id=request.target_server_id,
            target_path=request.target_path,
            strategy_requested=request.strategy,
            strategy_selected=self.selected,
            reason=self.reason,
        )


class Scenario:
    """Workspace declarations + DistributionService + SyncPlanner over fakes."""

    def __init__(self, tmp_path: Path, settings: Settings | None = None) -> None:
        self.settings = settings or Settings(data_dir=tmp_path / "data")
        repo = WorkspaceRepository(JsonFileStore(tmp_path / "workspace.json"))
        self.workspace = WorkspaceService(repo, server_exists=lambda _sid: True)
        self.factory = ExecutorFactoryFake()
        self.active_jobs: list[TransferJob] = []
        self.distribution = DistributionService(
            self.settings,
            self.workspace,
            executor_for=self.factory,
            active_transfers=lambda: self.active_jobs,
        )
        self.plans = PlanRecorder()
        self.planner = SyncPlanner(
            self.workspace,
            self.distribution,
            executor_for=self.factory,
            plan_transfer=self.plans,
        )

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

    def add_project(self, name: str, artifact_ids: list[str]) -> str:
        project = self.workspace.create_project(ProjectCreate(name=name, artifact_ids=artifact_ids))
        return project.project_id

    def set_roots(
        self,
        server_id: str,
        *,
        dataset_root: str | None = None,
        model_root: str | None = None,
        project_root: str | None = None,
    ) -> None:
        self.workspace.set_server_roots(
            server_id,
            ServerRootsUpdate(
                dataset_root=dataset_root, model_root=model_root, project_root=project_root
            ),
        )


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


async def _missing_dataset_scenario(tmp_path: Path) -> tuple[Scenario, str, str]:
    """Dataset declared on srv-tgt (checked MISSING) and on srv-src (usable)."""
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    target_placement = sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    source_placement = sc.add_placement(artifact_id, "srv-src", "/datasets/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-tgt"] = FakeExecutor(missing_paths={"/data/IVMSD"})
    sc.factory.script["srv-src"] = FakeExecutor()
    await sc.distribution.inspect_placement(target_placement)
    return sc, project_id, source_placement


# ---- 1. verified dataset -> SKIP ---------------------------------------------------


async def test_verified_dataset_is_skipped(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    placement_id = sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-tgt"] = FakeExecutor()
    await sc.distribution.inspect_placement(placement_id)  # cache says VERIFIED

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.SKIP
    assert item.target_status is DistributionState.VERIFIED
    assert item.strategy_selected is None
    assert item.source_server_id is None
    assert plan.valid and plan.error == ""
    assert sc.plans.requests == []  # nothing to transfer: planner never consulted


# ---- 2. missing dataset -> TRANSFER -------------------------------------------------


async def test_missing_dataset_transfers_from_selected_source(tmp_path: Path) -> None:
    sc, project_id, source_placement = await _missing_dataset_scenario(tmp_path)

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.TRANSFER
    assert item.target_status is DistributionState.MISSING
    assert item.source_placement_id == source_placement
    assert item.source_server_id == "srv-src"
    assert item.source_path == "/datasets/IVMSD"
    assert item.target_path == "/data/IVMSD"  # declared target path is reused
    assert "missing" in item.reason
    assert item.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert item.alternatives == []
    assert len(sc.plans.requests) == 1
    assert sc.plans.requests[0].source_placement_id == source_placement
    assert sc.plans.requests[0].target_path == "/data/IVMSD"
    assert plan.valid and plan.error == ""
    # the source preflight ran the fixed symlink probe against the source
    preflight_commands = [
        command for command in sc.factory.script["srv-src"].commands if '-L "$p"' in command
    ]
    assert len(preflight_commands) == 1
    assert "/datasets/IVMSD" in preflight_commands[0]


# ---- 3. verified code, refresh=False -> SKIP -----------------------------------------


async def test_verified_code_without_refresh_is_skipped(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("code", "trainer")
    placement_id = sc.add_placement(artifact_id, "srv-tgt", "/code/trainer")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-tgt"] = FakeExecutor()
    await sc.distribution.inspect_placement(placement_id)

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.SKIP
    assert "refresh not requested" in item.reason
    assert sc.plans.requests == []


# ---- 4. verified code, refresh=True -> TRANSFER ---------------------------------------


async def test_verified_code_with_refresh_transfers(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("code", "trainer")
    target_placement = sc.add_placement(artifact_id, "srv-tgt", "/code/trainer")
    source_placement = sc.add_placement(artifact_id, "srv-src", "/repo/trainer")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-tgt"] = FakeExecutor()
    sc.factory.script["srv-src"] = FakeExecutor()
    await sc.distribution.inspect_placement(target_placement)

    request = SyncPlanRequest(target_server_id="srv-tgt", refresh_code=True)
    plan = await sc.planner.build_plan(project_id, request)
    item = plan.items[0]
    assert item.action is SyncAction.TRANSFER
    assert item.target_status is DistributionState.VERIFIED
    assert item.source_placement_id == source_placement
    assert item.target_path == "/code/trainer"
    assert "refresh" in item.reason
    assert item.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert len(sc.plans.requests) == 1


# ---- 5. no usable source -> UNRESOLVED -------------------------------------------------


async def test_no_usable_source_is_unresolved(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    dead = sc.add_artifact("dataset", "DEAD")  # only source sits on a dead server
    target_dead = sc.add_placement(dead, "srv-tgt", "/data/DEAD")
    sc.add_placement(dead, "srv-dead", "/data/DEAD")
    lonely = sc.add_artifact("dataset", "LONELY")  # no source anywhere
    target_lonely = sc.add_placement(lonely, "srv-tgt", "/data/LONELY")
    project_id = sc.add_project("CMOS", [dead, lonely])
    sc.factory.script["srv-tgt"] = FakeExecutor(missing_paths={"/data/DEAD", "/data/LONELY"})
    sc.factory.unknown = {"srv-dead"}
    sc.set_roots("srv-tgt", dataset_root="/data")
    await sc.distribution.inspect_placement(target_dead)
    await sc.distribution.inspect_placement(target_lonely)

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    by_artifact = {item.artifact_id: item for item in plan.items}
    dead_item = by_artifact[dead]
    assert dead_item.action is SyncAction.UNRESOLVED
    assert "no usable source" in dead_item.reason
    assert "srv-dead" in dead_item.reason
    lonely_item = by_artifact[lonely]
    assert lonely_item.action is SyncAction.UNRESOLVED
    assert "no other server declares this artifact" in lonely_item.reason
    assert sc.plans.requests == []


# ---- 6. unusable source A falls through to usable B ------------------------------------


async def test_unusable_source_falls_to_next_and_lists_alternatives(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    target_placement = sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    sc.add_placement(artifact_id, "srv-a", "/data/A")  # server unknown -> unusable
    placement_b = sc.add_placement(artifact_id, "srv-b", "/data/B")  # usable -> selected
    sc.add_placement(artifact_id, "srv-c", "/data/C")  # usable -> alternative
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.unknown = {"srv-a"}
    sc.factory.script = {
        "srv-tgt": FakeExecutor(missing_paths={"/data/IVMSD"}),
        "srv-b": FakeExecutor(),
        "srv-c": FakeExecutor(),
    }
    sc.set_roots("srv-tgt", dataset_root="/data")
    await sc.distribution.inspect_placement(target_placement)

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.TRANSFER
    assert item.source_server_id == "srv-b"
    assert item.source_placement_id == placement_b
    assert item.alternatives == ["srv-c:/data/C"]
    assert "srv-b" in item.reason
    assert len(sc.plans.requests) == 1


# ---- 7. root symlink candidates are never selected --------------------------------------


async def test_root_symlink_source_is_rejected(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    target_placement = sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    sc.add_placement(artifact_id, "srv-a", "/data/A")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script = {
        "srv-tgt": FakeExecutor(missing_paths={"/data/IVMSD"}),
        "srv-a": FakeExecutor(preflight="SYMLINK"),
    }
    sc.set_roots("srv-tgt", dataset_root="/data")
    await sc.distribution.inspect_placement(target_placement)

    # a symlinked root is unusable: with no other candidate the item degrades
    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.UNRESOLVED
    assert "symlink" in item.reason
    assert sc.plans.requests == []

    # with a healthy candidate available, the symlinked source is skipped
    sc.add_placement(artifact_id, "srv-b", "/data/B")
    sc.factory.script["srv-b"] = FakeExecutor()
    plan2 = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item2 = plan2.items[0]
    assert item2.action is SyncAction.TRANSFER
    assert item2.source_server_id == "srv-b"
    assert all(not alt.startswith("srv-a:") for alt in item2.alternatives)


# ---- 8. target root not configured -> UNRESOLVED -----------------------------------------


async def test_missing_target_root_is_unresolved(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    sc.add_placement(artifact_id, "srv-src", "/datasets/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-src"] = FakeExecutor()
    request = SyncPlanRequest(target_server_id="srv-tgt")

    # no ServerRoots record at all for srv-tgt
    plan = await sc.planner.build_plan(project_id, request)
    item = plan.items[0]
    assert item.action is SyncAction.UNRESOLVED
    assert item.reason == "target root not configured on server"
    assert item.target_status is DistributionState.MISSING  # no declared placement
    assert item.target_path is None
    assert "srv-tgt" not in sc.factory.requested  # no SSH wasted on the target
    assert sc.plans.requests == []

    # an all-empty ServerRoots record is equally unusable
    sc.set_roots("srv-tgt")
    plan2 = await sc.planner.build_plan(project_id, request)
    assert plan2.items[0].reason == "target root not configured on server"


# ---- 9. unsafe display names are sanitized into the target leaf ---------------------------


async def test_target_leaf_is_sanitized(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "My:Data/X Y")
    sc.add_placement(artifact_id, "srv-src", "/datasets/x")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-src"] = FakeExecutor()
    sc.set_roots("srv-tgt", dataset_root="/data")

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.TRANSFER
    assert item.target_path == "/data/My-Data-X-Y"


# ---- 10. two versions of one name get separate leaves --------------------------------------


async def test_versions_get_separate_leaves(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    v1 = sc.add_artifact("dataset", "IVMSD", "v1")
    v2 = sc.add_artifact("dataset", "IVMSD", "v2-clean")
    sc.add_placement(v1, "srv-src", "/datasets/IVMSD-v1")
    sc.add_placement(v2, "srv-src", "/datasets/IVMSD-v2")
    project_id = sc.add_project("CMOS", [v1, v2])
    sc.factory.script["srv-src"] = FakeExecutor()
    sc.set_roots("srv-tgt", dataset_root="/data")

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    paths = {item.artifact_id: item.target_path for item in plan.items}
    assert paths[v1] == "/data/IVMSD--v1"
    assert paths[v2] == "/data/IVMSD--v2-clean"
    assert all(item.action is SyncAction.TRANSFER for item in plan.items)
    assert plan.valid  # separate leaves never overlap


# ---- 11. deterministic source selection; VERIFIED sources win -------------------------------


async def test_source_selection_is_deterministic_and_prefers_verified(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    target_placement = sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    sc.add_placement(artifact_id, "srv-a", "/data/A")  # declared first
    placement_b = sc.add_placement(artifact_id, "srv-b", "/data/B")  # declared second
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script = {
        "srv-tgt": FakeExecutor(missing_paths={"/data/IVMSD"}),
        "srv-a": FakeExecutor(),
        "srv-b": FakeExecutor(),
    }
    sc.set_roots("srv-tgt", dataset_root="/data")
    await sc.distribution.inspect_placement(target_placement)

    request = SyncPlanRequest(target_server_id="srv-tgt")
    plan1 = await sc.planner.build_plan(project_id, request)
    plan2 = await sc.planner.build_plan(project_id, request)
    assert plan1.items == plan2.items  # identical inputs -> identical decisions
    assert plan1.items[0].source_server_id == "srv-a"  # ties break by creation order

    # a VERIFIED source wins over creation order
    await sc.distribution.inspect_placement(placement_b)
    plan3 = await sc.planner.build_plan(project_id, request)
    assert plan3.items[0].source_server_id == "srv-b"


# ---- 12. the runtime transfer planner is reused exactly once per TRANSFER item ---------------


async def test_transfer_planner_is_reused_once_per_transfer_item(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    d1 = sc.add_artifact("dataset", "D1")
    m1 = sc.add_artifact("model", "M1")
    d2 = sc.add_artifact("dataset", "D2")  # will be verified -> SKIP
    sc.factory.script = {
        "srv-tgt": FakeExecutor(missing_paths={"/data/D1", "/data/M1"}),
        "srv-src": FakeExecutor(),
    }
    for artifact_id, name in ((d1, "D1"), (m1, "M1"), (d2, "D2")):
        target = sc.add_placement(artifact_id, "srv-tgt", f"/data/{name}")
        sc.add_placement(artifact_id, "srv-src", f"/datasets/{name}")
        await sc.distribution.inspect_placement(target)
    project_id = sc.add_project("CMOS", [d1, m1, d2])
    sc.set_roots("srv-tgt", dataset_root="/data", model_root="/models")

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    actions = {item.artifact_id: item.action for item in plan.items}
    assert actions[d1] is SyncAction.TRANSFER
    assert actions[m1] is SyncAction.TRANSFER
    assert actions[d2] is SyncAction.SKIP
    assert sorted(request.artifact_id for request in sc.plans.requests) == sorted([d1, m1])
    assert all(request.target_server_id == "srv-tgt" for request in sc.plans.requests)


# ---- 13. overlapping TRANSFER target paths mark the plan invalid ------------------------------


async def test_overlapping_transfer_targets_invalidate_plan(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    data = sc.add_artifact("dataset", "Data")  # suggested path /data/Data
    cache = sc.add_artifact("dataset", "DataCache")  # declared path /data/Data/cache
    cache_target = sc.add_placement(cache, "srv-tgt", "/data/Data/cache")
    sc.add_placement(data, "srv-src", "/datasets/Data")
    sc.add_placement(cache, "srv-src", "/datasets/DataCache")
    project_id = sc.add_project("CMOS", [data, cache])
    sc.factory.script = {
        "srv-tgt": FakeExecutor(missing_paths={"/data/Data/cache"}),
        "srv-src": FakeExecutor(),
    }
    sc.set_roots("srv-tgt", dataset_root="/data")
    await sc.distribution.inspect_placement(cache_target)  # MISSING -> TRANSFER there

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    actions = {item.artifact_id: item.action for item in plan.items}
    assert actions[data] is SyncAction.TRANSFER
    assert actions[cache] is SyncAction.TRANSFER
    assert plan.valid is False
    assert "/data/Data" in plan.error
    assert "/data/Data/cache" in plan.error
    assert plan.items  # decisions are still reported alongside the conflict


# ---- 14. DECLARED targets are explicitly checked during planning -------------------------------


async def test_declared_target_is_inspected_during_plan(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.script["srv-tgt"] = FakeExecutor()
    assert sc.factory.requested == []  # nothing checked yet

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert "srv-tgt" in sc.factory.requested  # the declared target was checked
    assert '-e "$p"' in sc.factory.script["srv-tgt"].commands[0]  # inspection command
    assert item.target_status is DistributionState.VERIFIED
    assert item.action is SyncAction.SKIP
    # the explicit check populated the shared observation cache
    dist = sc.distribution.distribution(project_id)
    assert dist.items[0].state is DistributionState.VERIFIED


# ---- 15. artifact_ids outside the project are rejected ------------------------------------------


async def test_foreign_artifact_ids_are_rejected(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    outsider = sc.add_artifact("dataset", "Other")  # exists, but in no project
    request = SyncPlanRequest(target_server_id="srv-tgt")

    with pytest.raises(ConflictError):
        await sc.planner.build_plan(
            project_id, SyncPlanRequest(target_server_id="srv-tgt", artifact_ids=[outsider])
        )
    with pytest.raises(ConflictError):
        await sc.planner.build_plan(
            project_id, SyncPlanRequest(target_server_id="srv-tgt", artifact_ids=["ghost"])
        )
    with pytest.raises(NotFoundError):
        await sc.planner.build_plan("ghost-project", request)


# ---- extra: SYNCING target -> SKIP with warning; UNAVAILABLE target -> UNRESOLVED ---------------


async def test_syncing_target_is_skipped_with_warning(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.active_jobs.append(_job("job-1", artifact_id, "srv-tgt", TransferState.RUNNING))

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.SKIP
    assert item.target_status is DistributionState.SYNCING
    assert item.warnings
    assert sc.plans.requests == []


async def test_unavailable_target_is_unresolved(tmp_path: Path) -> None:
    sc = Scenario(tmp_path)
    artifact_id = sc.add_artifact("dataset", "IVMSD")
    sc.add_placement(artifact_id, "srv-tgt", "/data/IVMSD")
    project_id = sc.add_project("CMOS", [artifact_id])
    sc.factory.unknown = {"srv-tgt"}

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.UNRESOLVED
    assert item.target_status is DistributionState.UNAVAILABLE
    assert sc.plans.requests == []


# ---- extra: no available strategy downgrades the item to UNRESOLVED ------------------------------


async def test_unavailable_strategy_downgrades_to_unresolved(tmp_path: Path) -> None:
    sc, project_id, _source_placement = await _missing_dataset_scenario(tmp_path)
    sc.plans.selected = None
    sc.plans.reason = "direct rsync unavailable: rsync missing on source or target"

    plan = await sc.planner.build_plan(project_id, SyncPlanRequest(target_server_id="srv-tgt"))
    item = plan.items[0]
    assert item.action is SyncAction.UNRESOLVED
    assert item.reason == "direct rsync unavailable: rsync missing on source or target"
    assert item.strategy_selected is None
    assert len(sc.plans.requests) == 1  # the planner WAS consulted before degrading


# ---- 16. API smoke ------------------------------------------------------------------------


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
    """executor_for double for the API harness (mirrors test_distribution)."""

    def __init__(self, registry: ServerRegistry) -> None:
        self._registry = registry
        self.requested: list[str] = []

    def __call__(self, server_id: str) -> FakeExecutor:
        self.requested.append(server_id)
        try:
            self._registry.get(server_id)
        except Exception as exc:
            raise ServerUnavailableError("server is unknown, deleted or disabled") from exc
        return FakeExecutor()


class Harness:
    """API harness mirroring test_distribution.Harness, with the sync planner
    and the transfer service wired into the context."""

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
        self.distribution = DistributionService(
            self.settings,
            self.workspace,
            executor_for=self.factory,
            active_transfers=lambda: [],
        )
        self.transfers = TransferService(
            settings=self.settings,
            ssh=FakeSsh(),  # type: ignore[arg-type]
            workspace=self.workspace,
        )
        self.plans = PlanRecorder()
        self.planner = SyncPlanner(
            self.workspace,
            self.distribution,
            executor_for=self.factory,
            plan_transfer=self.plans,
        )
        context = AppContext(
            self.settings,
            ssh=FakeSsh(),  # type: ignore[arg-type]
            telemetry=telemetry,
            workspace=self.workspace,
            transfers=self.transfers,
            distribution=self.distribution,
            sync_planner=self.planner,
        )
        self.client = TestClient(create_app(self.settings, context=context))

    def _names(self) -> set[str]:
        return {server.display_name for server in self.registry.all()}

    def _ids(self) -> set[str]:
        return {server.server_id for server in self.registry.all()}

    async def _telemetry_executor(self, _sid: str, _record: Any) -> FakeExecutor:
        return FakeExecutor()


@pytest.fixture()
def harness(tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(tmp_path)
    with harness.client as client:
        _ = client
        yield harness


def test_api_sync_plan_smoke(harness: Harness) -> None:
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
    inspected = client.post(
        f"/api/v1/workspace/placements/{placement.json()['placement_id']}/inspect"
    )
    assert inspected.json()["state"] == "verified"
    project_id = project["project_id"]

    got = client.post(
        f"/api/v1/workspace/projects/{project_id}/sync-plan",
        json={"target_server_id": server_id},
    )
    assert got.status_code == 200
    body = got.json()
    assert body["project_id"] == project_id
    assert body["target_server_id"] == server_id
    assert body["refresh_code"] is False
    assert body["valid"] is True
    assert body["error"] == ""
    assert body["items"][0]["artifact_id"] == artifact["artifact_id"]
    assert body["items"][0]["action"] == "skip"
    assert body["items"][0]["target_status"] == "verified"
    assert harness.plans.requests == []

    # artifact ids outside the project -> typed 409
    foreign = client.post(
        "/api/v1/workspace/artifacts", json={"kind": "code", "name": "Other"}
    ).json()
    bad = client.post(
        f"/api/v1/workspace/projects/{project_id}/sync-plan",
        json={"target_server_id": server_id, "artifact_ids": [foreign["artifact_id"]]},
    )
    assert bad.status_code == 409

    # unknown project -> typed 404
    missing = client.post(
        "/api/v1/workspace/projects/ghost/sync-plan",
        json={"target_server_id": server_id},
    )
    assert missing.status_code == 404

"""Project distribution service: explicit inspection + zero-SSH read model.

Owns the single `PlacementInspector` (and therefore the only RAM cache of
placement observations). Explicit operations (`inspect_project`,
`inspect_placement`) run bounded-concurrency SSH checks; `distribution()`
builds the read model purely from workspace declarations + the observation
cache + active transfer jobs — it never touches an executor.

Dependency injection keeps this module decoupled: `executor_for` builds an
executor per server_id (the factory raises for unknown/disabled servers;
this service turns that into UNAVAILABLE), `active_transfers` provides the
current transfer jobs. Importing `app.transfer.*` here would create a
circular dependency, so job types come from the pure `app.models` contracts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.collectors.base import ExecutorLike
from app.core.config import Settings
from app.models.distribution import (
    ArtifactServerDistribution,
    DistributionState,
    ProjectDistribution,
)
from app.models.transfer import TransferJob, TransferState
from app.models.workspace import (
    ArtifactRecord,
    InspectionState,
    PlacementInspection,
    PlacementRecord,
    ProjectRecord,
)
from app.workspace.inspection import PlacementInspector
from app.workspace.repository import utc_now
from app.workspace.service import WorkspaceService

__all__ = ["DistributionService", "ServerUnavailableError"]

ExecutorFactory = Callable[[str], ExecutorLike]  # server_id -> executor
ActiveTransfers = Callable[[], list[TransferJob]]

# Jobs in these states are actively mutating a target path; anything terminal
# (completed/failed/cancelled) must not overlay the distribution.
_ACTIVE_JOB_STATES = frozenset(
    {TransferState.QUEUED, TransferState.PLANNING, TransferState.RUNNING, TransferState.VERIFYING}
)

_OBSERVATION_STATE: dict[InspectionState, DistributionState] = {
    InspectionState.VERIFIED: DistributionState.VERIFIED,
    InspectionState.MISSING: DistributionState.MISSING,
    InspectionState.UNAVAILABLE: DistributionState.UNAVAILABLE,
    InspectionState.DECLARED: DistributionState.DECLARED,
}


class ServerUnavailableError(Exception):
    """Raised by `executor_for` when a server is unknown, deleted or disabled.

    The service converts any factory failure into an UNAVAILABLE observation;
    this type exists so factories can produce a precise, user-facing detail.
    """


class DistributionService:
    """Explicit distribution checks + project distribution read model."""

    def __init__(
        self,
        settings: Settings,
        workspace: WorkspaceService,
        executor_for: ExecutorFactory,
        active_transfers: ActiveTransfers,
        inspector: PlacementInspector | None = None,
    ) -> None:
        self._settings = settings
        self._workspace = workspace
        self._executor_for = executor_for
        self._active_transfers = active_transfers
        self._inspector = inspector if inspector is not None else PlacementInspector()

    # ---- explicit inspection (SSH) --------------------------------------------

    async def inspect_project(self, project_id: str) -> ProjectDistribution:
        """Explicitly check every declared placement of the project, then
        return the fresh distribution read model."""
        project = self._workspace.project(project_id)  # NotFoundError -> 404
        placements = self._project_placements(project)
        semaphore = asyncio.Semaphore(self._settings.project_inspection_concurrency)
        # return_exceptions: every placement gets its chance; the first typed
        # failure (e.g. a shell-hostile path) surfaces after the batch, so one
        # bad placement cannot strand the others mid-flight.
        results = await asyncio.gather(
            *(self._inspect_bounded(placement, semaphore) for placement in placements),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return self.distribution(project_id)

    async def inspect_placement(self, placement_id: str) -> PlacementInspection:
        """Check a single placement; shares the observation cache with
        project checks."""
        placement = self._workspace.placement(placement_id)  # NotFoundError -> 404
        return await self._inspect_one(placement)

    async def _inspect_bounded(
        self, placement: PlacementRecord, semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            await self._inspect_one(placement)

    async def _inspect_one(self, placement: PlacementRecord) -> PlacementInspection:
        try:
            executor = self._executor_for(placement.server_id)
        except Exception as exc:
            # Unknown/disabled/deleted server (the factory rejects before any
            # executor exists) or any factory failure: record, never crash.
            return self._inspector.record(
                PlacementInspection(
                    placement_id=placement.placement_id,
                    state=InspectionState.UNAVAILABLE,
                    detail=str(exc)[:200],
                    checked_at=utc_now(),
                )
            )
        return await self._inspector.inspect(executor, placement)

    # ---- read model (ZERO SSH) --------------------------------------------------

    def distribution(self, project_id: str) -> ProjectDistribution:
        """Read model over declarations + cache + active transfers only.

        Never calls `executor_for`: reading the matrix must not touch SSH.
        """
        project = self._workspace.project(project_id)
        jobs = list(self._active_transfers())
        items: list[ArtifactServerDistribution] = []
        for artifact_id in dict.fromkeys(project.artifact_ids):
            artifact = self._workspace.artifact(artifact_id)
            for placement in self._workspace.placements_of(artifact_id):
                items.append(self._item(artifact_id, artifact, placement, jobs))
        return ProjectDistribution(
            project_id=project.project_id, generated_at=utc_now(), items=items
        )

    def _item(
        self,
        artifact_id: str,
        artifact: ArtifactRecord,
        placement: PlacementRecord,
        jobs: list[TransferJob],
    ) -> ArtifactServerDistribution:
        observation = self._inspector.latest(placement.placement_id)
        if observation is None:
            state, checked_at, detail = DistributionState.DECLARED, None, None
        else:
            state = _OBSERVATION_STATE.get(observation.state, DistributionState.DECLARED)
            checked_at = observation.checked_at or None
            detail = observation.detail or None
        overlay = _syncing_job(placement, jobs)
        label = f"{artifact.name}:{artifact.version}" if artifact.version else artifact.name
        return ArtifactServerDistribution(
            artifact_id=artifact_id,
            artifact_label=label,
            artifact_kind=artifact.kind.value,
            server_id=placement.server_id,
            placement_id=placement.placement_id,
            remote_path=placement.remote_path,
            state=DistributionState.SYNCING if overlay is not None else state,
            checked_at=checked_at,
            detail=detail,
            active_transfer_job_id=overlay.job_id if overlay is not None else None,
        )

    def _project_placements(self, project: ProjectRecord) -> list[PlacementRecord]:
        placements: list[PlacementRecord] = []
        for artifact_id in dict.fromkeys(project.artifact_ids):
            placements.extend(self._workspace.placements_of(artifact_id))
        return placements


def _syncing_job(placement: PlacementRecord, jobs: list[TransferJob]) -> TransferJob | None:
    for job in jobs:
        if (
            job.state in _ACTIVE_JOB_STATES
            and job.artifact_id == placement.artifact_id
            and job.target_server_id == placement.server_id
        ):
            return job
    return None

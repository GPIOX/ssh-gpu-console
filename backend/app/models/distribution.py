"""Project distribution read models (Sol-owned contract).

Pure read models over declared placements plus RAM observations (inspection
cache, active transfer jobs). Never persisted; the frontend distribution
matrix renders unplaced artifact/server cells itself from the project's
artifact list. `extra="forbid"` everywhere.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.models.transfer import TransferStrategy


class DistributionState(StrEnum):
    DECLARED = "declared"  # placement metadata exists, unchecked on this runtime
    VERIFIED = "verified"  # latest explicit check confirmed the remote path
    MISSING = "missing"  # latest explicit check found no remote path
    UNAVAILABLE = "unavailable"  # server unknown/disabled or SSH failure
    SYNCING = "syncing"  # an active transfer job targets this placement (overlay)


class ArtifactServerDistribution(BaseModel):
    """One declared placement of one artifact on one server."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_label: str  # "name" or "name:version" for display
    artifact_kind: str
    server_id: str
    placement_id: str | None = None  # items always carry a declared placement
    remote_path: str
    state: DistributionState
    checked_at: str | None = None
    detail: str | None = None  # e.g. why the placement is unavailable
    active_transfer_job_id: str | None = None  # set only while SYNCING


class ProjectDistribution(BaseModel):
    """Distribution snapshot for one project; items are declared placements only."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    generated_at: str
    items: list[ArtifactServerDistribution]


class SyncAction(StrEnum):
    SKIP = "skip"  # nothing to do: target already in the desired state
    TRANSFER = "transfer"  # a transfer from a selected source would fix the target
    UNRESOLVED = "unresolved"  # no safe automatic decision (reason explains)


class ArtifactSyncItem(BaseModel):
    """One artifact's sync decision toward the plan's target server."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_label: str
    artifact_kind: str
    target_status: DistributionState  # target placement state at decision time
    action: SyncAction
    reason: str
    source_placement_id: str | None = None
    source_server_id: str | None = None
    source_path: str | None = None
    target_path: str | None = None
    strategy_selected: TransferStrategy | None = None  # TRANSFER items only
    alternatives: list[str] = Field(default_factory=list)  # other usable sources
    warnings: list[str] = Field(default_factory=list, max_length=20)


class ProjectSyncPlan(BaseModel):
    """Plan-level sync decisions for one project toward one target server."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    target_server_id: str
    generated_at: str
    refresh_code: bool
    items: list[ArtifactSyncItem]
    valid: bool = True
    error: str = ""  # plan-level error, e.g. overlapping TRANSFER target paths


class SyncPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_server_id: str = Field(min_length=1, max_length=64)
    # None = every artifact of the project; a provided list must be a subset
    # of the project's artifacts (else 409).
    artifact_ids: list[str] | None = Field(default=None, max_length=64)
    refresh_code: bool = False
    strategy: TransferStrategy = TransferStrategy.AUTO

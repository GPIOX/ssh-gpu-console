"""Cross-server transfer wire models (Sol-owned contract).

TransferJob is a runtime object: RAM only, never persisted to workspace.json.

Phase 3 additions: TransferJob carries run bookkeeping — `immutable` (dataset/
model targets must not be mutated), `strategy_reason` (copy of plan.reason for
display), `resumed_bytes` (bytes taken over from an existing partial),
`files_skipped`/`bytes_skipped` (incremental sync skips), `warnings` (bounded
list, e.g. skipped symlink reasons) — and TransferPlan carries the preflight
target disk probe (`target_free_b`, None = not probed) plus `space_warning`
for the plan preview UI.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

_PATH = Field(min_length=1, max_length=512)


class TransferStrategy(StrEnum):
    AUTO = "auto"
    DIRECT_RSYNC = "direct_rsync"
    LOCAL_RELAY = "local_relay"


class TransferState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class VerifyMode(StrEnum):
    QUICK = "quick"


class TransferPlan(BaseModel):
    """Result of POST /transfers/plan — no job is created by planning."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    source_server_id: str
    source_path: str
    target_server_id: str
    target_path: str
    strategy_requested: TransferStrategy = TransferStrategy.AUTO
    strategy_available: dict[str, bool] = Field(
        default_factory=dict, description="direct_rsync / local_relay availability"
    )
    strategy_selected: TransferStrategy | None = None
    reason: str = ""
    source_exists: bool | None = None
    source_size_b: int | None = None
    # Effective exclusions (union of the referencing projects' defaults) shown
    # in the plan preview so the user sees what will NOT be copied.
    excludes: list[str] = Field(default_factory=list, max_length=32)
    # Preflight target disk probe (None = not probed) and the "not enough
    # space" warning text for the plan preview UI.
    target_free_b: int | None = None
    space_warning: str = ""


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(min_length=1, max_length=64)
    source_placement_id: str = Field(min_length=1, max_length=64)
    target_server_id: str = Field(min_length=1, max_length=64)
    target_path: str = Field(min_length=1, max_length=512)
    strategy: TransferStrategy = TransferStrategy.AUTO
    verify_mode: VerifyMode = VerifyMode.QUICK


class TransferJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)
    artifact_id: str
    artifact_label: str = ""  # "name:version" for display

    source_server_id: str
    source_path: str
    target_server_id: str
    target_path: str

    strategy_requested: TransferStrategy
    strategy_used: TransferStrategy | None = None
    state: TransferState = TransferState.QUEUED
    excludes: list[str] = Field(default_factory=list, max_length=32)

    # Phase 3 run bookkeeping (all defaulted so existing constructors keep
    # working); the service fills these at create/run time.
    immutable: bool = False  # dataset/model: target must not be mutated; code may
    strategy_reason: str = ""  # copy of plan.reason for job detail views
    resumed_bytes: int = 0  # bytes carried over from an existing partial file
    files_skipped: int = 0  # incremental sync: files already up-to-date on target
    bytes_skipped: int = 0  # incremental sync: bytes already up-to-date on target
    warnings: list[str] = Field(default_factory=list, max_length=20)  # e.g. skipped symlinks

    bytes_total: int | None = None
    bytes_done: int = 0
    files_total: int | None = None
    files_done: int = 0
    rate_bps: float | None = None
    eta_s: float | None = None
    current_path: str | None = None

    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    error_code: str | None = None
    error_message: str = ""

"""Cross-server transfer wire models (Sol-owned contract).

TransferJob is a runtime object: RAM only, never persisted to workspace.json.
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

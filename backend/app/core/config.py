"""Local control-plane settings. No secrets live here."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SGC_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8420
    data_dir: Path = DATA_DIR

    # SSH behaviour
    connect_timeout_s: float = 15.0
    command_timeout_s: float = 10.0
    max_in_flight_global: int = 8
    max_in_flight_per_server: int = 2
    reconnect_backoff_min_s: float = 2.0
    reconnect_backoff_max_s: float = 60.0
    keepalive_interval_s: float = 15.0

    # Sampling (seconds)
    fleet_interval_active: float = 15.0
    fleet_interval_idle: float = 45.0
    fast_interval: float = 2.5
    medium_interval: float = 5.0
    slow_interval: float = 30.0
    process_interval: float = 7.5
    static_interval: float = 600.0
    idle_grace_s: float = 20.0

    # History
    history_points: int = 300

    # Transfers (isolated from telemetry concurrency)
    max_transfers_global: int = Field(default=2, ge=1, le=16)
    max_transfers_per_server: int = Field(default=1, ge=1, le=8)
    transfer_chunk_size_b: int = Field(default=4 * 1024 * 1024, ge=65536, le=64 * 1048576)
    transfer_job_history: int = Field(default=100, ge=10, le=1000)
    transfer_progress_emit_interval_s: float = Field(default=0.5, ge=0.1, le=5.0)
    transfer_command_timeout_s: float = Field(default=3600.0, ge=30.0, le=86400.0)
    transfer_preflight_timeout_s: float = Field(default=15.0, ge=5.0, le=120.0)

    # Realtime
    client_queue_size: int = 2

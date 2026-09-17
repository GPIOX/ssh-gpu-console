"""Shared wire models for the server registry and connection testing.

These models are Sol-owned contracts. Collectors, APIs, and the frontend all
speak this vocabulary. Wire-facing models reject unknown fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ServerRecord(BaseModel):
    """A managed server definition as persisted in the registry."""

    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    ssh_host: str = Field(min_length=1, max_length=255, description="alias or hostname")
    username: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    # Optional per-server detail sampling override (seconds). Only applies to
    # the selected-server FAST tier; fleet cadence stays global. None = default.
    sample_interval_s: float | None = Field(default=None, ge=0.5, le=600.0)


class ServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=120)
    ssh_host: str = Field(min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    sample_interval_s: float | None = Field(default=None, ge=0.5, le=600.0)


class ServerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    ssh_host: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    tags: list[str] | None = None
    enabled: bool | None = None
    sample_interval_s: float | None = Field(default=None, ge=0.5, le=600.0)


class AliasEntry(BaseModel):
    """One usable host entry parsed from the user's ~/.ssh/config."""

    model_config = ConfigDict(extra="forbid")

    alias: str
    host: str | None = None
    user: str | None = None
    port: int | None = None


class HostKeyPrompt(BaseModel):
    """An untrusted host key surfaced for explicit user consent (TOFU).

    Trust is stored in an app-owned file; the user's ~/.ssh/known_hosts is
    never modified by this application.
    """

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int | None = None
    key_type: str
    fingerprint: str  # sha256 format, e.g. "SHA256:...."


class ProcessActionRequest(BaseModel):
    """Body for named process actions. Nothing else — no shell, no signals."""

    model_config = ConfigDict(extra="forbid")

    pid: int = Field(ge=1, le=4_194_304)


class ActionResult(BaseModel):
    """Domain-level outcome of a named action (HTTP stays 200 unless 4xx)."""

    model_config = ConfigDict(extra="forbid")

    action: str  # "terminate_process" | "kill_process"
    pid: int
    ok: bool
    status: str  # ok | not_found | permission_denied | failed | <ServerStatus value>
    detail: str = ""


class ConnectionTestResult(BaseModel):
    """Outcome of an explicit connection test for onboarding."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    status: str  # ServerStatus value; on failure one of the failure states
    detail: str
    latency_ms: float | None = None
    pending_host_key: HostKeyPrompt | None = None

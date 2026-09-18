"""Server→server direct-transfer auth models (Phase 4.2C, wire contracts).

Two separate auth planes exist BY DESIGN and never share material:

- LOCAL auth: console→server (Phase 4.2A, optional password) — unrelated here.
- DIRECT auth: source server→target server for DIRECT_RSYNC. A password is
  NEVER used for it. Priority: existing native BatchMode SSH (zero config) >
  an SGC dedicated transfer key (explicit user click) > unavailable (Local
  Relay fallback).

``DirectAuthStatus`` is the result of an EXPLICIT SSH check; it carries
reasons and booleans only — never secrets, key material or paths.
``DirectAuthPairMetadata`` is the persistence record of a configured
dedicated key pair (paths on the REMOTE machines, fingerprint — no secrets).
``DirectAuthPairView`` is the zero-SSH projection served by the list route.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class DirectAuthMethod(StrEnum):
    """How the source server can SSH to the target non-interactively."""

    NATIVE = "native"  # existing BatchMode key/agent on the source: nothing to configure
    SGC_KEY = "sgc_key"  # SGC-owned dedicated transfer key installed by setup-key


class DirectAuthReason(StrEnum):
    """Failure taxonomy for direct-auth checks (reason codes, not messages)."""

    ROUTE_UNREACHABLE = "route_unreachable"
    HOST_KEY_UNKNOWN = "host_key_unknown"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    AUTHENTICATION_FAILED = "authentication_failed"
    RSYNC_MISSING_SOURCE = "rsync_missing_source"
    RSYNC_MISSING_TARGET = "rsync_missing_target"
    DEDICATED_KEY_MISSING = "dedicated_key_missing"
    AUTHORIZED_KEY_MISSING = "authorized_key_missing"
    REMOTE_KEY_INVALID = "remote_key_invalid"
    SOURCE_KNOWN_HOSTS_MISSING = "source_known_hosts_missing"
    KEYGEN_MISSING_SOURCE = "keygen_missing_source"
    UNKNOWN = "unknown"


class DirectAuthStatus(BaseModel):
    """Outcome of an explicit check / setup-key / revoke action."""

    model_config = ConfigDict(extra="forbid")

    source_server_id: str
    target_server_id: str
    configured: bool = False
    method: DirectAuthMethod | None = None
    available: bool | None = None
    reason: str | None = None
    checked_at: str | None = None


class DirectAuthPairMetadata(BaseModel):
    """Persisted record of ONE configured dedicated-key pair (setup item 27).

    Paths are on the REMOTE source machine (relative to its home). The
    fingerprint identifies the authorized public key; private key material is
    never represented here (it never leaves the source server).
    """

    model_config = ConfigDict(extra="forbid")

    source_server_id: str
    target_server_id: str
    key_id: str
    remote_private_key_path: str
    remote_public_key_path: str
    remote_known_hosts_path: str
    public_key_fingerprint: str
    created_at: str


class DirectAuthPairView(BaseModel):
    """Zero-SSH projection of one configured pair (+ latest RAM check result)."""

    model_config = ConfigDict(extra="forbid")

    source_server_id: str
    target_server_id: str
    configured: bool
    method: DirectAuthMethod | None = None
    available: bool | None = None
    reason: str | None = None
    checked_at: str | None = None


class DirectAuthList(BaseModel):
    """Pinned list shape for GET /servers/{id}/direct-auth (zero-SSH)."""

    model_config = ConfigDict(extra="forbid")

    server_id: str
    pairs: list[DirectAuthPairView] = Field(default_factory=list)

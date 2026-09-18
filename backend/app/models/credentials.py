"""Wire models for optional SSH password credentials and local auth posture.

``ServerPasswordSet`` carries the ONLY secret on the wire (``SecretStr``:
Pydantic guarantees its repr/JSON never expose the value). No response model
anywhere contains a password; ``AuthStatus`` exposes structure counts and
storage-mode labels only — never key paths or credential material.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ServerPasswordSet(BaseModel):
    """PUT body for /servers/{id}/credentials/password."""

    model_config = ConfigDict(extra="forbid")

    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordCredentialStatus(BaseModel):
    """Whether a password is configured and where it lives (mode label only)."""

    model_config = ConfigDict(extra="forbid")

    configured: bool
    storage: str | None = None  # a CredentialStorageMode value; None when unconfigured


class AuthStatus(BaseModel):
    """Local SSH auth posture for one server. Counts only — no key paths."""

    model_config = ConfigDict(extra="forbid")

    ssh_config_used: bool
    effective_host: str
    effective_user: str | None
    effective_port: int
    identity_files: int  # count of usable IdentityFile entries
    agent_available: bool
    proxy_jump_configured: bool
    password_configured: bool
    password_storage: str | None  # a CredentialStorageMode value; None when unconfigured

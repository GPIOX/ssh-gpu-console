"""Credential-store contract: optional per-server SSH passwords as secrets.

A password is a SECRET. It is only ever held as ``SecretStr`` here; every
implementation must keep it out of logs, files, repr() and error messages.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import SecretStr


class CredentialStorageMode(StrEnum):
    """Where a configured password physically lives (safe to expose to the UI)."""

    SYSTEM_KEYRING = "system_keyring"
    SESSION_ONLY = "session_only"
    UNAVAILABLE = "unavailable"


class CredentialStoreError(RuntimeError):
    """A credential-store operation failed.

    The message carries ONLY the operation kind, never the secret, never the
    account payload — safe to surface as an HTTP 409 detail.
    """


class CredentialStore(Protocol):
    """Read/write access to optional per-server SSH passwords.

    Implementations must be synchronous and must never persist secrets to
    JSON/configuration files; the OS keyring (when secure) or process RAM are
    the only permitted sinks.
    """

    def has_password(self, server_id: str) -> bool:
        """Whether a password is configured for the server."""
        ...

    def get_password(self, server_id: str) -> SecretStr | None:
        """The stored password, or None when absent/unknown."""
        ...

    def set_password(self, server_id: str, password: SecretStr) -> None:
        """Store or replace the password; raises CredentialStoreError on failure."""
        ...

    def delete_password(self, server_id: str) -> None:
        """Remove the password; unknown ids are a no-op unless the backend fails."""
        ...

    def storage_mode(self) -> CredentialStorageMode:
        """Backend identifier reported to the UI (never the secret itself)."""
        ...

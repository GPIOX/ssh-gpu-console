"""Session-only RAM credential store (no secure OS keyring available).

Everything lives in one process-local dict: nothing is written anywhere, and
a backend restart loses every stored password — the UI may present this mode
as "仅本次会话".
"""

from __future__ import annotations

from pydantic import SecretStr

from app.credentials.base import CredentialStorageMode


class SessionMemoryCredentialStore:
    """Dict-in-RAM store; deletion of an unknown server_id is a no-op."""

    def __init__(self) -> None:
        self._passwords: dict[str, SecretStr] = {}

    def has_password(self, server_id: str) -> bool:
        return server_id in self._passwords

    def get_password(self, server_id: str) -> SecretStr | None:
        return self._passwords.get(server_id)

    def set_password(self, server_id: str, password: SecretStr) -> None:
        self._passwords[server_id] = password

    def delete_password(self, server_id: str) -> None:
        self._passwords.pop(server_id, None)

    def storage_mode(self) -> CredentialStorageMode:
        return CredentialStorageMode.SESSION_ONLY

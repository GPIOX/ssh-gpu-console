"""Optional per-server SSH password credentials (Phase 4.2A).

The password is a SECRET: it lives only in a ``CredentialStore`` (OS keyring
or session RAM), is transported as ``SecretStr``, and is unwrapped exactly
once — at the asyncssh connect call site in ``app.ssh.transport``. It never
reaches registry/workspace JSON, logs, models, API responses or URLs.
"""

from __future__ import annotations

from app.credentials.base import CredentialStorageMode, CredentialStore, CredentialStoreError
from app.credentials.keyring import (
    SystemKeyringCredentialStore,
    credential_names,
    detect_secure_backend,
)
from app.credentials.memory import SessionMemoryCredentialStore
from app.credentials.store import (
    build_credential_store,
    get_credential_store,
    set_credential_store,
)

__all__ = [
    "CredentialStorageMode",
    "CredentialStore",
    "CredentialStoreError",
    "SessionMemoryCredentialStore",
    "SystemKeyringCredentialStore",
    "build_credential_store",
    "credential_names",
    "detect_secure_backend",
    "get_credential_store",
    "set_credential_store",
]

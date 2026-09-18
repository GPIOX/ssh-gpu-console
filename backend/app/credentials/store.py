"""Credential-store factory and process-wide accessors.

Tests (and the UI layer) swap the store via ``set_credential_store`` — the
same pattern as the servers registry. ``get_credential_store`` lazily builds
the best available store: the OS keyring when a genuinely secure backend is
detected, otherwise the session-only RAM store (never a plaintext file).
"""

from __future__ import annotations

import threading

from app.credentials.base import CredentialStore
from app.credentials.keyring import SystemKeyringCredentialStore, detect_secure_backend
from app.credentials.memory import SessionMemoryCredentialStore

_store: CredentialStore | None = None
_lock = threading.Lock()


def build_credential_store() -> CredentialStore:
    """Secure OS keyring when available; RAM-only session store otherwise."""

    if detect_secure_backend():
        return SystemKeyringCredentialStore()
    return SessionMemoryCredentialStore()


def get_credential_store() -> CredentialStore:
    global _store
    with _lock:
        if _store is None:
            _store = build_credential_store()
        return _store


def set_credential_store(store: CredentialStore | None) -> None:
    """Install (or clear, reverting to lazy build) the process-wide store."""

    global _store
    with _lock:
        _store = store

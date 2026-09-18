"""OS-keyring credential store with STRICT secure-backend detection.

Detection allows ONLY genuinely secure OS backends: macOS Keychain, the Linux
Secret Service, and Windows Credential Manager. Everything else — fail/null
backends, chainer aggregates, and any file-based/plaintext backend — is NOT
secure and must never receive a password. There is no plaintext-file
fallback: without a secure backend the factory returns the session RAM store.

Keyring namespace (centralized in ``credential_names``):
    service = "ssh-gpu-console"
    account = "server:<server_id>:password"   # never the hostname alone
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from app.core.logging import get_logger
from app.credentials.base import CredentialStorageMode, CredentialStoreError

logger = get_logger("credentials.keyring")

KEYRING_SERVICE = "ssh-gpu-console"
_ACCOUNT_PREFIX = "server:"
_ACCOUNT_SUFFIX = ":password"

# (module, class) pairs considered genuinely secure. macOS ships the backend
# as ``Keychain`` in some keyring releases and as ``Keyring`` in others (the
# class was renamed around keyring 24/25); the MODULE is the macOS-only
# signal, so both names are accepted there. Every other module is rejected.
_SECURE_BACKENDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("keyring.backends.macOS", "Keychain"),
        ("keyring.backends.macOS", "Keyring"),
        ("keyring.backends.SecretService", "Keyring"),
    }
)
_SECURE_MODULE_PREFIXES = ("keyring.backends.Windows",)


def credential_names(server_id: str) -> tuple[str, str]:
    """Fixed keyring ``(service, account)`` for one server; deterministic."""

    return KEYRING_SERVICE, f"{_ACCOUNT_PREFIX}{server_id}{_ACCOUNT_SUFFIX}"


def detect_secure_backend() -> bool:
    """True only when ``keyring`` resolves to a known-secure OS backend.

    Any import failure, probe error, or unrecognized/fail/null/chainer/file
    backend yields False. Pure function of the environment: no writes happen
    here.
    """

    try:
        import keyring
    except Exception:  # import failure -> not secure
        return False
    try:
        backend: Any = keyring.get_keyring()
    except Exception:  # probe error -> not secure
        return False
    module = type(backend).__module__
    name = type(backend).__name__
    if (module, name) in _SECURE_BACKENDS:
        return True
    return module.startswith(_SECURE_MODULE_PREFIXES)


class SystemKeyringCredentialStore:
    """Stores passwords in the OS keychain through the ``keyring`` library.

    The plain-text value exists only inside ``set_password`` for the duration
    of the keyring library call. Failures raise ``CredentialStoreError`` whose
    message names only the operation — the secret never appears in errors.
    """

    def __init__(self) -> None:
        if not detect_secure_backend():
            raise CredentialStoreError("no secure keyring backend available")

    def storage_mode(self) -> CredentialStorageMode:
        return CredentialStorageMode.SYSTEM_KEYRING

    def has_password(self, server_id: str) -> bool:
        return self.get_password(server_id) is not None

    def get_password(self, server_id: str) -> SecretStr | None:
        import keyring

        service, account = credential_names(server_id)
        try:
            value = keyring.get_password(service, account)
        except Exception as error:
            raise CredentialStoreError(f"keyring read failed: {type(error).__name__}") from error
        return None if value is None else SecretStr(value)

    def set_password(self, server_id: str, password: SecretStr) -> None:
        import keyring

        service, account = credential_names(server_id)
        try:
            keyring.set_password(service, account, password.get_secret_value())
        except Exception as error:
            raise CredentialStoreError(f"keyring write failed: {type(error).__name__}") from error

    def delete_password(self, server_id: str) -> None:
        import keyring

        service, account = credential_names(server_id)
        try:
            keyring.delete_password(service, account)
        except Exception as error:
            raise CredentialStoreError(f"keyring delete failed: {type(error).__name__}") from error

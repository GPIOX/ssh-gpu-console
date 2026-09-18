"""Credential-store and password-aware transport tests (Phase 4.2A).

Only SYNTHETIC secrets are used; the real keyring is never touched (all
keyring interactions are monkeypatched with dict-backed fakes).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.credentials.base import CredentialStorageMode, CredentialStoreError
from app.credentials.keyring import (
    KEYRING_SERVICE,
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
from app.models.server import ServerRecord
from app.ssh.config_resolver import ConnectParams, SSHConfigResolver
from app.ssh.known_hosts import HostKeyTrust
from app.ssh.manager import SshManager
from app.ssh.transport import make_connect_factory
from pydantic import SecretStr

SECRET = "SGC_SUPER_SECRET_TEST_928361"


def _secret() -> SecretStr:
    return SecretStr(SECRET)


# ---- SessionMemoryCredentialStore -------------------------------------------


def test_session_store_roundtrip_and_unknown_id_noop() -> None:
    store = SessionMemoryCredentialStore()
    assert store.storage_mode() is CredentialStorageMode.SESSION_ONLY
    assert not store.has_password("srv-1")
    assert store.get_password("srv-1") is None

    store.set_password("srv-1", _secret())
    assert store.has_password("srv-1")
    stored = store.get_password("srv-1")
    assert stored is not None and stored.get_secret_value() == SECRET

    store.delete_password("srv-1")
    assert not store.has_password("srv-1")
    store.delete_password("never-existed")  # unknown id: no-op
    assert not store.has_password("never-existed")


def test_session_store_is_ram_only_and_loses_data_on_reinstantiation() -> None:
    store = SessionMemoryCredentialStore()
    store.set_password("srv-1", _secret())
    fresh = SessionMemoryCredentialStore()  # a "restarted backend"
    assert not fresh.has_password("srv-1")
    assert fresh.get_password("srv-1") is None


def test_session_store_never_writes_files(tmp_path: Path) -> None:
    store = SessionMemoryCredentialStore()
    store.set_password("srv-1", _secret())
    store.delete_password("srv-1")
    assert list(tmp_path.rglob("*")) == []


# ---- keyring naming ---------------------------------------------------------


def test_credential_names_are_deterministic_and_never_hostname_alone() -> None:
    assert credential_names("srv-abc123") == (KEYRING_SERVICE, "server:srv-abc123:password")
    assert KEYRING_SERVICE == "ssh-gpu-console"
    for _ in range(3):  # deterministic across calls
        assert credential_names("srv-abc123") == ("ssh-gpu-console", "server:srv-abc123:password")
    account = credential_names("web01")[1]
    assert account != "web01"  # never the bare id/hostname
    assert account.startswith("server:") and account.endswith(":password")


# ---- secure-backend detection (all stubbed; no real keyring I/O) ------------


def _backend_class(module: str, name: str) -> type:
    return type(name, (), {"__module__": module})


def _detect_with(monkeypatch: pytest.MonkeyPatch, backend: type) -> bool:
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: backend(), raising=False)
    return detect_secure_backend()


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("keyring.backends.fail", "Keyring"),
        ("keyring.backends.null", "Keyring"),
        ("keyring.backends.chainer", "ChainerBackend"),
        ("keyrings.alt.file.PlaintextKeyring", "PlaintextKeyring"),
        ("keyring.backends.file", "Keyring"),
        ("keyring.backends.kwallet", "Keyring"),
    ],
)
def test_detect_rejects_insecure_backends(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str
) -> None:
    assert _detect_with(monkeypatch, _backend_class(module_name, class_name)) is False


def test_detect_rejects_probe_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import keyring

    def _boom() -> Any:
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(keyring, "get_keyring", _boom, raising=False)
    assert detect_secure_backend() is False


def test_detect_rejects_missing_keyring_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    assert detect_secure_backend() is False


@pytest.mark.parametrize(
    "module_name,class_name",
    [
        ("keyring.backends.macOS", "Keychain"),
        ("keyring.backends.macOS", "Keyring"),  # class renamed in keyring >= 24
        ("keyring.backends.SecretService", "Keyring"),
        ("keyring.backends.Windows", "WinVaultKeyring"),
    ],
)
def test_detect_accepts_secure_os_backends(
    monkeypatch: pytest.MonkeyPatch, module_name: str, class_name: str
) -> None:
    assert _detect_with(monkeypatch, _backend_class(module_name, class_name)) is True


# ---- SystemKeyringCredentialStore (dict-backed fake keyring) ----------------


class _FakeKeyringModule:
    """In-memory stand-in for the `keyring` module API."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}
        self.fail_on: set[str] = set()

    def get_password(self, service: str, account: str) -> str | None:
        return self.data.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        if "set" in self.fail_on:
            raise RuntimeError("backend unavailable")
        self.data[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        if "delete" in self.fail_on:
            raise RuntimeError("backend unavailable")
        self.data.pop((service, account), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyringModule:
    import keyring

    fake = _FakeKeyringModule()
    monkeypatch.setattr(keyring, "get_password", fake.get_password, raising=False)
    monkeypatch.setattr(keyring, "set_password", fake.set_password, raising=False)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password, raising=False)
    monkeypatch.setattr("app.credentials.keyring.detect_secure_backend", lambda: True)
    return fake


def test_keyring_store_roundtrip_uses_fixed_namespace(
    fake_keyring: _FakeKeyringModule,
) -> None:
    store = SystemKeyringCredentialStore()
    assert store.storage_mode() is CredentialStorageMode.SYSTEM_KEYRING

    store.set_password("srv-1", _secret())
    assert ("ssh-gpu-console", "server:srv-1:password") in fake_keyring.data
    assert store.has_password("srv-1")
    stored = store.get_password("srv-1")
    assert stored is not None and stored.get_secret_value() == SECRET
    assert store.has_password("other") is False

    store.delete_password("srv-1")
    assert not store.has_password("srv-1")
    store.delete_password("never-existed")  # absent id: no-op


def test_keyring_store_failures_raise_secret_free_errors(
    fake_keyring: _FakeKeyringModule,
) -> None:
    fake_keyring.fail_on.add("set")
    fake_keyring.fail_on.add("delete")
    store = SystemKeyringCredentialStore()

    with pytest.raises(CredentialStoreError) as set_error:
        store.set_password("srv-1", _secret())
    assert "keyring write failed" in str(set_error.value)
    assert SECRET not in str(set_error.value)

    with pytest.raises(CredentialStoreError) as delete_error:
        store.delete_password("srv-1")
    assert "keyring delete failed" in str(delete_error.value)
    assert SECRET not in str(delete_error.value)


def test_keyring_store_rejects_insecure_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.credentials.keyring.detect_secure_backend", lambda: False)
    with pytest.raises(CredentialStoreError):
        SystemKeyringCredentialStore()


# ---- factory and accessors --------------------------------------------------


def test_build_credential_store_selects_by_backend_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.credentials.store.detect_secure_backend", lambda: True)
    monkeypatch.setattr("app.credentials.keyring.detect_secure_backend", lambda: True)
    assert isinstance(build_credential_store(), SystemKeyringCredentialStore)

    monkeypatch.setattr("app.credentials.store.detect_secure_backend", lambda: False)
    assert isinstance(build_credential_store(), SessionMemoryCredentialStore)


def test_credential_store_accessors_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    session = SessionMemoryCredentialStore()
    set_credential_store(session)
    try:
        assert get_credential_store() is session
    finally:
        set_credential_store(None)

    # Cleared store lazily rebuilds; insecure detection -> session store.
    monkeypatch.setattr("app.credentials.store.detect_secure_backend", lambda: False)
    rebuilt = get_credential_store()
    assert isinstance(rebuilt, SessionMemoryCredentialStore)
    set_credential_store(None)


# ---- local SSH integration --------------------------------------------------


def test_connect_params_has_no_password_field() -> None:
    # The password must NEVER become a ConnectParams field.
    assert "password" not in ConnectParams.__dataclass_fields__


def test_manager_params_attach_server_id(tmp_path: Path) -> None:
    manager = SshManager(
        Settings(data_dir=tmp_path),
        resolver=SSHConfigResolver(tmp_path / "missing_ssh_config"),
        trust_store=HostKeyTrust(tmp_path / "trusted.json"),
        connect_factory=_unused_factory,
    )
    server = ServerRecord(server_id="srv-9", display_name="s9", ssh_host="host-9")
    params = manager.resolve_params_for(server)
    assert params.server_id == "srv-9"
    assert manager._params(server).server_id == "srv-9"
    # The transfer planner's resolve_params_for stays password-free:
    assert not hasattr(params, "password")


async def _unused_factory(_params: ConnectParams) -> Any:
    raise AssertionError("unused")


class _CapturingAsyncssh:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("FakeConnection", (), {})()


def _identity_file(tmp_path: Path) -> Path:
    path = tmp_path / "id_test"
    path.write_text("")
    return path


def _factory_params(tmp_path: Path, server_id: str | None) -> ConnectParams:
    return ConnectParams(
        host="10.0.0.10",
        port=2222,
        username="deploy",
        identity_files=[_identity_file(tmp_path)],
        proxy_jump="jump@10.0.0.9:2200",
        source="ssh_config",
        server_id=server_id,
    )


async def test_real_factory_forwards_password_to_asyncssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ssh.transport as transport

    capturing = _CapturingAsyncssh()
    monkeypatch.setattr(transport.asyncssh, "connect", capturing)
    trust = HostKeyTrust(tmp_path / "trusted.json")
    store = SessionMemoryCredentialStore()
    factory = make_connect_factory(Settings(data_dir=tmp_path), trust, credentials=store)

    # No credential yet: the password key must be ABSENT.
    await factory(_factory_params(tmp_path, "srv-1"))

    store.set_password("srv-1", _secret())
    await factory(_factory_params(tmp_path, "srv-1"))

    # Credential for another server: still absent.
    await factory(_factory_params(tmp_path, "srv-2"))

    # No server_id on params: lookup skipped entirely.
    await factory(_factory_params(tmp_path, None))

    assert "password" not in capturing.calls[0]
    second = capturing.calls[1]
    assert second["password"] == SECRET
    # Other kwargs stay intact alongside the injected password.
    assert second["host"] == "10.0.0.10"
    assert second["port"] == 2222
    assert second["username"] == "deploy"
    assert second["client_keys"] == [str(_identity_file(tmp_path))]
    assert second["tunnel"] == "jump@10.0.0.9:2200"
    assert second["config"] == []
    assert "known_hosts" in second
    assert "password" not in capturing.calls[2]  # other server's credential
    assert "password" not in capturing.calls[3]  # params without server_id


async def test_factory_without_credentials_never_looks_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.ssh.transport as transport

    capturing = _CapturingAsyncssh()
    monkeypatch.setattr(transport.asyncssh, "connect", capturing)

    factory = make_connect_factory(Settings(data_dir=tmp_path), HostKeyTrust(tmp_path / "t.json"))
    await factory(_factory_params(tmp_path, "srv-1"))
    assert "password" not in capturing.calls[0]


class _FakeManagedConnection:
    """Minimal RemoteConnection stand-in for manager-level retire checks."""

    def __init__(self) -> None:
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    async def run(self, _command: str, **_kwargs: Any) -> Any:
        return type("R", (), {"exit_status": 0, "stdout": "", "stderr": ""})()


async def test_password_change_closes_existing_connection(tmp_path: Path) -> None:
    """Same retire pattern the API route uses: set/delete -> close_server."""

    connections: list[_FakeManagedConnection] = []

    async def factory(_params: ConnectParams) -> _FakeManagedConnection:
        connection = _FakeManagedConnection()
        connections.append(connection)
        return connection

    store = SessionMemoryCredentialStore()
    manager = SshManager(
        Settings(data_dir=tmp_path),
        resolver=SSHConfigResolver(tmp_path / "missing"),
        trust_store=HostKeyTrust(tmp_path / "trusted.json"),
        connect_factory=factory,
        credentials=store,
    )
    server = ServerRecord(server_id="srv-1", display_name="s1", ssh_host="host-1")

    await manager.run(server, "cmd")
    assert len(connections) == 1

    store.set_password("srv-1", _secret())
    await manager.close_server("srv-1")  # exactly what the PUT route does
    assert connections[0].closed
    assert await manager.connection_stats() == []

    await manager.run(server, "cmd")  # reconnects; factory sees the server_id
    assert len(connections) == 2
    await manager.close_all()

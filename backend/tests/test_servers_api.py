"""Registry and /api/v1/servers API tests: fake manager, no SSH involved."""

from __future__ import annotations

import contextlib
import os
import stat as stat_module
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.v1.servers import router
from app.core.config import Settings
from app.core.errors import ConflictError
from app.credentials.base import CredentialStorageMode, CredentialStoreError
from app.credentials.memory import SessionMemoryCredentialStore
from app.credentials.store import set_credential_store
from app.models.credentials import ServerPasswordSet
from app.models.server import (
    ConnectionTestResult,
    HostKeyPrompt,
    ServerCreate,
    ServerPatch,
    ServerRecord,
)
from app.models.telemetry import ServerStatus
from app.persistence.json_store import JsonFileStore
from app.runtime import Runtime, set_runtime
from app.servers.discovery import SSHConfigDiscovery, set_default_discovery
from app.servers.registry import ServerRegistry, get_default_registry, set_default_registry
from app.ssh.config_resolver import ConnectParams, SSHConfigResolver
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

SECRET = "SGC_SUPER_SECRET_TEST_928361"


class FakeSshManager:
    """Duck-typed stand-in for SshManager; records manager interactions."""

    def __init__(self) -> None:
        self.closed: list[str] = []
        self.test_calls = 0
        self.trust_calls: list[HostKeyPrompt] = []
        self.result = ConnectionTestResult(
            ok=True, status=ServerStatus.ONLINE.value, detail="connected", latency_ms=1.5
        )
        self.auth_params: ConnectParams | None = None

    def resolve_params_for(self, server: ServerRecord) -> ConnectParams:
        if self.auth_params is not None:
            return self.auth_params
        return ConnectParams(
            host=server.ssh_host,
            port=server.port or 22,
            username=server.username,
            source="default",
            server_id=server.server_id,
        )

    async def close_server(self, server_id: str) -> None:
        self.closed.append(server_id)

    async def test_connection(self, server: ServerRecord) -> ConnectionTestResult:
        del server
        self.test_calls += 1
        return self.result

    async def trust_host_key(
        self, server: ServerRecord, prompt: HostKeyPrompt
    ) -> ConnectionTestResult:
        del server
        self.trust_calls.append(prompt)
        return ConnectionTestResult(
            ok=True, status=ServerStatus.ONLINE.value, detail="trusted", latency_ms=2.5
        )


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, ServerRegistry, FakeSshManager]]:
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    set_default_registry(registry)

    config = tmp_path / "ssh_config"
    config.write_text("Host lab-box\n    HostName 10.0.0.10\n    User deploy\n    Port 2201\n")
    set_default_discovery(SSHConfigDiscovery(SSHConfigResolver(config)))

    manager = FakeSshManager()
    set_runtime(Runtime(settings=Settings(data_dir=tmp_path), ssh=manager, telemetry=object()))
    app = FastAPI()
    app.include_router(router)

    yield TestClient(app), registry, manager

    set_runtime(None)
    set_default_registry(None)
    set_default_discovery(None)


def _create(client: TestClient, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"display_name": "Lab", "ssh_host": "lab-host"}
    payload.update(overrides)
    response = client.post("/api/v1/servers", json=payload)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def test_crud_roundtrip_closes_connection_on_identity_change_and_delete(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, manager = api
    created = _create(client, tags=["gpu"])
    server_id = created["server_id"]
    assert created["display_name"] == "Lab"
    assert client.get("/api/v1/servers").json() == [created]

    renamed = client.patch(f"/api/v1/servers/{server_id}", json={"display_name": "Lab 2"})
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Lab 2"
    assert manager.closed == []  # display-name-only patch keeps the connection

    moved = client.patch(f"/api/v1/servers/{server_id}", json={"ssh_host": "lab-host-2"})
    assert moved.status_code == 200
    assert manager.closed == [server_id]  # identity change closed the connection

    disabled = client.patch(f"/api/v1/servers/{server_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert manager.closed == [server_id, server_id]

    deleted = client.delete(f"/api/v1/servers/{server_id}")
    assert deleted.status_code == 204
    assert manager.closed == [server_id, server_id, server_id]
    assert client.get("/api/v1/servers").json() == []


def test_create_conflict_and_unknown_id_and_extra_fields(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    _create(client, ssh_host="HostA", username="root", port=22)
    duplicate = client.post(
        "/api/v1/servers",
        json={"display_name": "Other", "ssh_host": "hosta", "username": "root", "port": 22},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "conflict"

    # No GET-single route exists by contract; exercise existing methods.
    assert client.post("/api/v1/servers/nope/test").status_code == 404
    assert client.patch("/api/v1/servers/nope", json={"display_name": "x"}).status_code == 404
    assert client.delete("/api/v1/servers/nope").status_code == 404

    extra = client.post(
        "/api/v1/servers", json={"display_name": "X", "ssh_host": "h", "bogus": True}
    )
    assert extra.status_code == 422


def test_ssh_config_aliases_endpoint(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    response = client.get("/api/v1/servers/ssh-config/aliases")
    assert response.status_code == 200
    assert response.json() == [
        {"alias": "lab-box", "host": "10.0.0.10", "user": "deploy", "port": 2201}
    ]


def test_test_connection_endpoint_maps_results(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]

    manager.result = ConnectionTestResult(
        ok=False,
        status=ServerStatus.HOST_KEY_ERROR.value,
        detail="host key is not trusted yet",
        pending_host_key=HostKeyPrompt(
            host="lab-host", port=22, key_type="ssh-ed25519", fingerprint="SHA256:abc"
        ),
    )
    failed = client.post(f"/api/v1/servers/{server_id}/test")
    assert failed.status_code == 200
    body = failed.json()
    assert body["ok"] is False
    assert body["status"] == "host_key_error"
    assert body["pending_host_key"]["fingerprint"] == "SHA256:abc"

    manager.result = ConnectionTestResult(
        ok=True, status=ServerStatus.ONLINE.value, detail="connected", latency_ms=3.25
    )
    ok = client.post(f"/api/v1/servers/{server_id}/test")
    assert ok.json()["status"] == "online"
    assert ok.json()["latency_ms"] == 3.25


def test_trust_endpoint_returns_retest_result(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]
    prompt = HostKeyPrompt(
        host="lab-host", port=22, key_type="ssh-ed25519", fingerprint="SHA256:abc"
    )
    response = client.post(f"/api/v1/servers/{server_id}/host-key/trust", json=prompt.model_dump())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "online",
        "detail": "trusted",
        "latency_ms": 2.5,
        "pending_host_key": None,
    }
    assert manager.trust_calls == [prompt]


def test_openapi_has_no_shell_or_command_endpoints(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    paths = client.get("/openapi.json").json()["paths"]
    joined = " ".join(paths).lower()
    assert "shell" not in joined
    assert "exec" not in joined
    assert "command" not in joined


# --- registry behavior ---


def test_registry_write_only_on_explicit_mutation_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    registry = ServerRegistry(JsonFileStore(path))
    created = registry.create(ServerCreate(display_name="Lab", ssh_host="lab"))
    first = path.read_text(encoding="utf-8")
    assert '"display_name": "Lab"' in first
    registry.update(created.server_id, ServerPatch(display_name="Lab 2"))
    assert path.read_text(encoding="utf-8") != first
    reloaded = ServerRegistry(JsonFileStore(path))
    assert reloaded.get(created.server_id).display_name == "Lab 2"


def test_corrupt_registry_is_not_renamed_at_startup(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text("{broken", encoding="utf-8")
    ServerRegistry(JsonFileStore(path))
    assert path.read_text(encoding="utf-8") == "{broken"
    assert list(tmp_path.glob("*.corrupt")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_duplicate_detection_is_casefolded(tmp_path: Path) -> None:
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    registry.create(ServerCreate(display_name="A", ssh_host="HostA", username="root", port=22))
    with pytest.raises(ConflictError):
        registry.create(ServerCreate(display_name="B", ssh_host="hosta", username="root", port=22))


def test_registry_rejects_shell_hostile_hosts(tmp_path: Path) -> None:
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    with pytest.raises(ValueError):
        registry.create(ServerCreate(display_name="X", ssh_host="host; touch /tmp/pwn"))


def test_registry_enforces_server_limit(tmp_path: Path) -> None:
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    for index in range(256):
        registry.create(ServerCreate(display_name=f"s{index}", ssh_host=f"host-{index}"))
    with pytest.raises(ConflictError):
        registry.create(ServerCreate(display_name="overflow", ssh_host="host-overflow"))


def test_default_registry_accessor_roundtrip(tmp_path: Path) -> None:
    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    set_default_registry(registry)
    try:
        assert get_default_registry() is registry
    finally:
        set_default_registry(None)
    assert get_default_registry() is not registry  # rebuilt lazily from settings
    set_default_registry(None)


# --- password credentials (Phase 4.2A) ---------------------------------------


@pytest.fixture
def credentials() -> Iterator[SessionMemoryCredentialStore]:
    store = SessionMemoryCredentialStore()
    set_credential_store(store)
    try:
        yield store
    finally:
        set_credential_store(None)


class _RaisingStore(SessionMemoryCredentialStore):
    """Store whose backend fails on one operation; messages carry no secret."""

    def __init__(self, fail: str) -> None:
        super().__init__()
        self._fail = fail  # "set" or "delete"

    def set_password(self, server_id: str, password: SecretStr) -> None:
        if self._fail == "set":
            raise CredentialStoreError("keyring write failed: BackendUnavailable")
        super().set_password(server_id, password)

    def delete_password(self, server_id: str) -> None:
        if self._fail == "delete":
            raise CredentialStoreError("keyring delete failed: BackendUnavailable")
        super().delete_password(server_id)


def _put_password(client: TestClient, server_id: str, password: str = SECRET) -> Any:
    return client.put(
        f"/api/v1/servers/{server_id}/credentials/password", json={"password": password}
    )


def test_password_set_and_delete_roundtrip_via_api(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]

    response = _put_password(client, server_id)
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": True, "storage": "session_only"}
    assert credentials.has_password(server_id)
    assert credentials.get_password(server_id) is not None
    assert credentials.get_password(server_id).get_secret_value() == SECRET  # type: ignore[union-attr]
    assert manager.closed == [server_id]  # connection retired for reconnect

    removed = client.delete(f"/api/v1/servers/{server_id}/credentials/password")
    assert removed.status_code == 200
    assert removed.json() == {"configured": False, "storage": None}
    assert not credentials.has_password(server_id)
    assert manager.closed == [server_id, server_id]


def test_password_delete_absent_is_fine(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]
    response = client.delete(f"/api/v1/servers/{server_id}/credentials/password")
    assert response.status_code == 200
    assert response.json() == {"configured": False, "storage": None}
    assert manager.closed == [server_id]


def test_password_and_auth_routes_404_for_unknown_server(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    assert client.get("/api/v1/servers/nope/auth").status_code == 404
    assert (
        client.put(
            "/api/v1/servers/nope/credentials/password", json={"password": SECRET}
        ).status_code
        == 404
    )
    assert client.delete("/api/v1/servers/nope/credentials/password").status_code == 404


def test_password_body_validation_rejects_empty_long_and_extra(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    server_id = _create(client)["server_id"]
    assert (
        client.put(
            f"/api/v1/servers/{server_id}/credentials/password", json={"password": ""}
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/v1/servers/{server_id}/credentials/password", json={"password": "x" * 1025}
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/v1/servers/{server_id}/credentials/password",
            json={"password": SECRET, "extra": True},
        ).status_code
        == 422
    )


def test_password_set_storage_failure_is_409_without_echo(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]
    set_credential_store(_RaisingStore("set"))

    response = _put_password(client, server_id)

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "password credential could not be stored"
    assert SECRET not in response.text
    assert credentials.has_password(server_id) is False  # nothing stored
    assert manager.closed == []  # no close on failure


def test_password_delete_storage_failure_is_409(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
) -> None:
    client, _registry, _manager = api
    server_id = _create(client)["server_id"]
    store = _RaisingStore("delete")
    store.set_password(server_id, ServerPasswordSet(password=SECRET).password)
    set_credential_store(store)

    response = client.delete(f"/api/v1/servers/{server_id}/credentials/password")

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "password credential could not be removed"
    assert SECRET not in response.text


def test_delete_server_removes_password_best_effort(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
) -> None:
    client, _registry, manager = api
    server_id = _create(client)["server_id"]
    _put_password(client, server_id)
    assert credentials.has_password(server_id)

    deleted = client.delete(f"/api/v1/servers/{server_id}")
    assert deleted.status_code == 204
    assert not credentials.has_password(server_id)  # best-effort cleanup ran
    assert manager.closed == [server_id, server_id]  # PUT + delete-server


def test_delete_server_succeeds_even_when_credential_cleanup_fails(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, registry, _manager = api
    server_id = _create(client)["server_id"]
    store = _RaisingStore("delete")
    store.set_password(server_id, ServerPasswordSet(password=SECRET).password)
    set_credential_store(store)

    with caplog.at_level("DEBUG"):
        deleted = client.delete(f"/api/v1/servers/{server_id}")

    assert deleted.status_code == 204  # deletion itself succeeded
    assert client.get("/api/v1/servers").json() == []
    assert registry.count() == 0
    assert any(
        "password credential cleanup failed" in record.getMessage() for record in caplog.records
    )
    assert SECRET not in caplog.text


def test_auth_status_reports_params_and_agent_socket(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    client, _registry, manager = api
    server_id = _create(client)["server_id"]
    identity_one = tmp_path / "id_ed25519"
    identity_two = tmp_path / "id_rsa"
    identity_one.write_text("")
    manager.auth_params = ConnectParams(
        host="10.0.0.10",
        port=2201,
        username="deploy",
        identity_files=[identity_one, identity_two],
        proxy_jump="jump@10.0.0.9:2222",
        source="ssh_config",
        server_id=server_id,
    )

    # A real AF_UNIX socket (bound, never connected to). The path must stay
    # short enough for sockaddr_un, so it lives in the system temp dir.
    socket_dir = tempfile.mkdtemp(prefix="sgc-agent-")
    agent_socket = Path(socket_dir) / "a.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(agent_socket))
        monkeypatch.setenv("SSH_AUTH_SOCK", str(agent_socket))
        assert stat_module.S_ISSOCK(agent_socket.stat().st_mode)

        status = client.get(f"/api/v1/servers/{server_id}/auth")
        assert status.status_code == 200, status.text
        assert status.json() == {
            "ssh_config_used": True,
            "effective_host": "10.0.0.10",
            "effective_user": "deploy",
            "effective_port": 2201,
            "identity_files": 2,
            "agent_available": True,
            "proxy_jump_configured": True,
            "password_configured": False,
            "password_storage": None,
        }
    finally:
        sock.close()
        with contextlib.suppress(OSError):
            os.unlink(agent_socket)
        with contextlib.suppress(OSError):
            os.rmdir(socket_dir)

    # No SSH_AUTH_SOCK: agent unavailable.
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert client.get(f"/api/v1/servers/{server_id}/auth").json()["agent_available"] is False

    # SSH_AUTH_SOCK pointing at a regular file: not a socket, unavailable.
    regular = tmp_path / "not-a-socket"
    regular.write_text("")
    monkeypatch.setenv("SSH_AUTH_SOCK", str(regular))
    assert client.get(f"/api/v1/servers/{server_id}/auth").json()["agent_available"] is False

    # Missing socket path: unavailable.
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "missing.sock"))
    assert client.get(f"/api/v1/servers/{server_id}/auth").json()["agent_available"] is False

    # Defaults: no ssh_config source, no proxy jump.
    manager.auth_params = None
    default_status = client.get(f"/api/v1/servers/{server_id}/auth").json()
    assert default_status["ssh_config_used"] is False
    assert default_status["proxy_jump_configured"] is False
    assert default_status["effective_host"] == "lab-host"

    # With a configured password the storage mode is surfaced.
    _put_password(client, server_id)
    configured = client.get(f"/api/v1/servers/{server_id}/auth").json()
    assert configured["password_configured"] is True
    assert configured["password_storage"] == CredentialStorageMode.SESSION_ONLY.value


def test_password_secret_never_leaks_through_api_or_disk(
    api: tuple[TestClient, ServerRegistry, FakeSshManager],
    credentials: SessionMemoryCredentialStore,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The leak battery: the PUT body secret appears NOWHERE — not in any API
    response, not in any JSON file under the data dir, not in repr(), and not
    in any log record emitted by the PUT/DELETE/delete-server flows."""

    client, _registry, _manager = api
    server_id = _create(client)["server_id"]

    with caplog.at_level("DEBUG"):
        stored = _put_password(client, server_id)
        listed = client.get("/api/v1/servers")
        auth = client.get(f"/api/v1/servers/{server_id}/auth")
        removed = client.delete(f"/api/v1/servers/{server_id}/credentials/password")
        _put_password(client, server_id)
        deleted = client.delete(f"/api/v1/servers/{server_id}")

    assert stored.status_code == 200
    assert removed.status_code == 200
    assert deleted.status_code == 204

    # (a) GET /servers: no secret, and no password field at all.
    for record in listed.json():
        assert SECRET not in str(record)
        assert "password" not in record
    # (b) GET auth status: no secret.
    assert SECRET not in auth.text
    # (e) logs over PUT/DELETE/delete-server flows: no secret anywhere.
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()

    # (c)+(f-scan) every *.json file under the tmp data dir: no secret.
    json_files = list(tmp_path.rglob("*.json"))
    assert json_files, "expected at least registry.json under the data dir"
    for path in json_files:
        assert SECRET not in path.read_text(encoding="utf-8", errors="replace"), str(path)

    # (d) repr/str/json of the wire model never expose the value.
    body = ServerPasswordSet(password=SecretStr(SECRET))
    assert SECRET not in repr(body)
    assert SECRET not in str(body)
    assert SECRET not in body.model_dump_json()
    assert SECRET not in str(body.password)
    assert repr(body.password) == "SecretStr('**********')"

    # (f) error path: store raising -> 409 response carries no secret.
    set_credential_store(_RaisingStore("set"))
    try:
        failure = _put_password(client, _create(client)["server_id"])
        assert failure.status_code == 409
        assert SECRET not in failure.text
        assert failure.json()["detail"]["message"] == "password credential could not be stored"
    finally:
        set_credential_store(credentials)

"""Registry and /api/v1/servers API tests: fake manager, no SSH involved."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.api.v1.servers import router
from app.core.config import Settings
from app.core.errors import ConflictError
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
from app.ssh.config_resolver import SSHConfigResolver
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeSshManager:
    """Duck-typed stand-in for SshManager; records manager interactions."""

    def __init__(self) -> None:
        self.closed: list[str] = []
        self.test_calls = 0
        self.trust_calls: list[HostKeyPrompt] = []
        self.result = ConnectionTestResult(
            ok=True, status=ServerStatus.ONLINE.value, detail="connected", latency_ms=1.5
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

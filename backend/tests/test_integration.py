"""End-to-end integration tests over the composed application.

Real registry + real TelemetryService/scheduler/hub + a scripted SSH layer
(no network). Verifies the full chain: CRUD → reconcile → collection →
fleet/WS delivery → named actions → health → SPA fallback.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.lifecycle import AppContext
from app.main import create_app
from app.models.telemetry import ServerStatus
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import RemoteCommandResult
from fastapi.testclient import TestClient

# ---- canned remote outputs ---------------------------------------------------

CPU_MEMORY_OUT = (
    "cpu  100 0 100 700 0 0 0 0 0 0\n"
    "cpu0 50 0 50 350 0 0 0 0 0 0\n"
    "intr 0\n"
    "ctxt 0\n"
    "btime 1700000000\n"
    "processes 0\n"
    "procs_running 1\n"
    "procs_blocked 0\n"
    "1.20 0.80 0.50 2/420 1234\n"
    "MemTotal:       16000000 kB\n"
    "MemFree:         4000000 kB\n"
    "MemAvailable:    8000000 kB\n"
    "Buffers:          200000 kB\n"
    "Cached:          2000000 kB\n"
    "SwapTotal:             0 kB\n"
    "SwapFree:              0 kB\n"
)

GPU_OUT = (
    "0,GPU-aaaa,NVIDIA GeForce RTX 4090,550.54.14,72,18000,24564,64,210,350,55\n"
    "1,GPU-bbbb,NVIDIA GeForce RTX 4090,550.54.14,3,900,24564,39,62,350,30\n"
)

DF_OUT = (
    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/nvme0n1p2 960000 480000 480000 50% /\n"
)

NET_OUT = (
    "Inter-|   Receive                                                |  Transmit\n"
    " face |bytes    packets errs drop fifo frame compressed multicast|bytes"
    "    packets errs drop fifo colls carrier compressed\n"
    "  eth0: 1000000 1000 0 0 0 0 0 0  2000000 800 0 0 0 0 0 0\n"
)

PS_OUT = (
    "  PID USER                 %CPU %MEM    RSS STAT COMMAND  ARGS\n"
    " 40211 demo                 85.0  4.2 500000   Rl  python   python train.py --epochs 10\n"
    "   421 root                 0.1  0.1  30000   Ss  systemd  /sbin/init\n"
)

SYSTEM_OUT = (
    "gpu-box-01\n"
    "6.8.0-45-generic\n"
    'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
    "184000.25\n"
    "16\n"
    "model name\t: Intel(R) Core(TM) i9-13900K\n"
    "NVIDIA Driver Version: 550.54.14   CUDA Version: 12.4\n"
)


def _respond(command: str, scenario: dict[str, str], tick: int) -> RemoteCommandResult:
    if command.startswith("kill -TERM"):
        return RemoteCommandResult(0, scenario.get("term", "rc=0\n"), "", 1.0)
    if command.startswith("kill -KILL"):
        return RemoteCommandResult(0, scenario.get("kill", "rc=0\n"), "", 1.0)
    if command.startswith("nvidia-smi --query-gpu"):
        return RemoteCommandResult(0, GPU_OUT, "", 2.0)
    if command.startswith("nvidia-smi --query-compute-apps"):
        return RemoteCommandResult(0, "40211,python,18000,GPU-aaaa\n", "", 2.0)
    if command.startswith("cat /proc/stat"):
        # Counters must advance between samples or the CPU parser's
        # counter-reset guard keeps utilization at None.
        user, idle = 100 + tick * 50, 700 + tick * 350
        cpu = f"cpu  {user} 0 100 {idle} 0 0 0 0 0 0\n"
        rest = CPU_MEMORY_OUT.split("\n", 1)[1]
        return RemoteCommandResult(0, cpu + rest, "", 1.0)
    if command.startswith("df -kP"):
        return RemoteCommandResult(0, DF_OUT, "", 2.0)
    if command.startswith("cat /proc/net/dev"):
        return RemoteCommandResult(0, NET_OUT, "", 1.0)
    if command.startswith("ps -eo"):
        return RemoteCommandResult(0, PS_OUT, "", 3.0)
    if command.startswith("hostname"):
        return RemoteCommandResult(0, SYSTEM_OUT, "", 2.0)
    return RemoteCommandResult(0, "", "", 0.5)


class FakeSshLayer:
    """Stands in for SshManager: run() answers from canned outputs."""

    def __init__(self) -> None:
        self.scenario: dict[str, str] = {}
        self.closed: list[str] = []
        self.tick = 0

    async def run(
        self, server: Any, command: str, *, timeout_s: float | None = None
    ) -> RemoteCommandResult:
        self.tick += 1
        return _respond(command, self.scenario, self.tick)

    async def close_server(self, server_id: str) -> None:
        self.closed.append(server_id)

    async def close_all(self) -> None:
        self.closed.append("*")


class FakeExecutor:
    def __init__(self, layer: FakeSshLayer) -> None:
        self._layer = layer

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        self._layer.tick += 1
        return _respond(command, self._layer.scenario, self._layer.tick)

    async def close(self) -> None:
        return None


# ---- fixtures ----------------------------------------------------------------


class Integration:
    def __init__(self, tmp_path: Path) -> None:
        # Short cadences so multi-sample assertions finish in seconds.
        self.settings = Settings(
            data_dir=tmp_path / "data",
            fleet_interval_active=0.8,
            fleet_interval_idle=1.6,
            fast_interval=0.5,
            medium_interval=1.0,
            process_interval=1.5,
            slow_interval=2.0,
            static_interval=5.0,
        )
        self.registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
        self.ssh = FakeSshLayer()
        self.telemetry = self._telemetry()
        self.client = TestClient(
            create_app(
                self.settings,
                context=AppContext(
                    self.settings,
                    ssh=self.ssh,  # type: ignore[arg-type]
                    telemetry=self.telemetry,
                ),
            )
        )

    def _telemetry(self) -> Any:
        from app.telemetry.service import TelemetryService

        async def factory(server_id: str, record: Any) -> FakeExecutor:
            return FakeExecutor(self.ssh)

        return TelemetryService(self.settings, self.registry, factory)


@pytest.fixture()
def integration(tmp_path: Path) -> Iterator[Integration]:
    harness = Integration(tmp_path)
    set_default_registry(harness.registry)
    with harness.client as client:
        harness.client = client
        yield harness
    set_default_registry(None)


def _add_server(harness: Integration, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "display_name": "lab-4090",
        "ssh_host": "lab-4090",
        "username": "demo",
        "port": 22,
    }
    payload.update(overrides)
    response = harness.client.post("/api/v1/servers", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ---- tests -------------------------------------------------------------------


def test_health_reports_resources(integration: Integration) -> None:
    data = integration.client.get("/api/v1/health").json()
    assert data["status"] == "ok"
    assert data["rss_kb"] > 0
    assert isinstance(data["asyncio_tasks"], int)
    assert data["scheduler_mode"] in ("interactive", "idle", "unknown")


def test_crud_reconciles_and_fleet_becomes_online(integration: Integration) -> None:
    record = _add_server(integration)
    server_id = record["server_id"]
    deadline = asyncio.get_event_loop().time() + 8.0
    entry = None
    while asyncio.get_event_loop().time() < deadline:
        summary = integration.client.get("/api/v1/telemetry/fleet").json()
        candidate = next((s for s in summary["servers"] if s["server_id"] == server_id), None)
        # The first sample carries no CPU delta yet; wait for the second.
        if (
            candidate is not None
            and candidate["status"] == ServerStatus.ONLINE.value
            and candidate["cpu_percent"] is not None
        ):
            entry = candidate
            assert entry["gpu_count"] == 2
            assert entry["gpu_busy"] + entry["gpu_free"] == 2
            break
        import time

        time.sleep(0.2)
    assert entry is not None

    snapshot = integration.client.get(f"/api/v1/telemetry/servers/{server_id}").json()
    assert snapshot["status"] == ServerStatus.ONLINE.value
    assert len(snapshot["gpus"]) == 2
    assert snapshot["gpus"][0]["vram_total_b"] == 24564 * 1024 * 1024
    assert snapshot["system"]["os_pretty"] == "Ubuntu 22.04.4 LTS"

    # Processes are a selected-server tier: simulate a live client, select,
    # then wait for one PROCESS-tier pass.
    integration.telemetry.note_client_connected()
    integration.telemetry.set_selected(server_id)
    deadline = asyncio.get_event_loop().time() + 6.0
    snapshot = integration.client.get(f"/api/v1/telemetry/servers/{server_id}").json()
    while asyncio.get_event_loop().time() < deadline and not snapshot["processes"]:
        import time

        time.sleep(0.2)
        snapshot = integration.client.get(f"/api/v1/telemetry/servers/{server_id}").json()
    integration.telemetry.set_selected(None)
    integration.telemetry.note_client_disconnected()
    assert any(p["pid"] == 40211 for p in snapshot["processes"])
    assert snapshot["gpu_processes"][0]["pid"] == 40211

    history = integration.client.get(
        f"/api/v1/telemetry/servers/{server_id}/gpu-history?gpu=0&metric=utilization"
    ).json()
    assert isinstance(history, list)

    delete = integration.client.delete(f"/api/v1/servers/{server_id}")
    assert delete.status_code == 204
    assert integration.registry.count() == 0


def test_terminate_process_success_and_classification(integration: Integration) -> None:
    record = _add_server(integration)
    server_id = record["server_id"]

    ok = integration.client.post(
        f"/api/v1/servers/{server_id}/actions/terminate-process", json={"pid": 40211}
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body == {
        "action": "terminate_process",
        "pid": 40211,
        "ok": True,
        "status": "ok",
        "detail": "signal delivered",
    }

    integration.ssh.scenario["term"] = "kill: (40211) - No such process\nrc=1\n"
    missing = integration.client.post(
        f"/api/v1/servers/{server_id}/actions/terminate-process", json={"pid": 40211}
    ).json()
    assert missing["ok"] is False and missing["status"] == "not_found"

    integration.ssh.scenario["kill"] = "kill: (40211) - Operation not permitted\nrc=1\n"
    denied = integration.client.post(
        f"/api/v1/servers/{server_id}/actions/kill-process", json={"pid": 40211}
    ).json()
    assert denied["ok"] is False and denied["status"] == "permission_denied"


def test_action_guards(integration: Integration) -> None:
    assert (
        integration.client.post(
            "/api/v1/servers/unknown/actions/kill-process", json={"pid": 5}
        ).status_code
        == 404
    )
    record = _add_server(integration, enabled=False)
    response = integration.client.post(
        f"/api/v1/servers/{record['server_id']}/actions/kill-process", json={"pid": 5}
    )
    assert response.status_code == 409
    injection = integration.client.post(
        f"/api/v1/servers/{record['server_id']}/actions/kill-process",
        json={"pid": 5, "shell": "rm -rf /"},
    )
    assert injection.status_code == 422


def test_openapi_has_no_shell_endpoints(integration: Integration) -> None:
    paths = integration.client.get("/openapi.json").json()["paths"]
    forbidden = ("/shell", "/exec", "/command")
    assert not any(any(f in path for f in forbidden) for path in paths)


def test_realtime_delivers_fleet_and_snapshot(integration: Integration) -> None:
    record = _add_server(integration)
    server_id = record["server_id"]
    with integration.client.websocket_connect("/api/v1/realtime") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        ws.send_json({"type": "select", "server_id": server_id})
        got_fleet = False
        got_server = False
        for _ in range(10):
            frame = ws.receive_json()
            if frame["type"] == "fleet" and frame["summary"]["servers"]:
                got_fleet = True
            if frame["type"] == "server" and frame["server_id"] == server_id:
                got_server = True
            if got_fleet and got_server:
                break
        assert got_fleet and got_server


def test_spa_fallback_serves_index_and_blocks_escape(integration: Integration) -> None:
    response = integration.client.get("/")
    if response.status_code == 404:  # frontend not built in this environment
        pytest.skip("frontend dist not built")
    assert "text/html" in response.headers["content-type"]
    assert integration.client.get("/../../etc/passwd").status_code in (404, 200)
    traversal = integration.client.get("/..%2F..%2Fetc%2Fpasswd")
    assert traversal.status_code == 404

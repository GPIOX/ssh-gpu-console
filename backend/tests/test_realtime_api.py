"""Realtime + telemetry API tests.

REST routes are tested against a duck-typed fake service; the WebSocket
protocol is tested against the real TelemetryService with scripted executors
and stub websockets (no SSH transport anywhere).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.v1.realtime import router as realtime_router
from app.api.v1.telemetry import router as telemetry_router
from app.core.config import Settings
from app.models.server import ServerRecord
from app.models.telemetry import (
    FleetEntry,
    FleetSummary,
    HistoryPoint,
    ServerSnapshot,
    ServerStatus,
)
from app.runtime import Runtime, set_runtime
from app.telemetry.service import TelemetryService
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient


def record(server_id: str) -> ServerRecord:
    return ServerRecord(
        server_id=server_id, display_name=server_id.upper(), ssh_host=f"{server_id}.lan"
    )


async def wait_until(
    predicate: Callable[[], bool], wait_seconds: float = 5.0, message: str = "condition not met"
) -> None:
    deadline = time.monotonic() + wait_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.01)


class FakeRegistry:
    def __init__(self, records: list[ServerRecord]) -> None:
        self.records = records

    def all(self) -> list[ServerRecord]:
        return list(self.records)


class ScriptedExecutor:
    """Minimal fake executor returning healthy outputs for every command.

    ``/proc/stat`` counters advance on every read so consecutive samples
    yield a real utilization delta instead of a counter reset.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def run(self, command: str, *, timeout_s: float = 10.0) -> Any:
        self.calls += 1
        return SimpleNamespace(
            exit_code=0, stdout=_STDOUT(command, self.calls), stderr="", duration_ms=1.0
        )

    async def close(self) -> None:
        self.closed = True


def _STDOUT(command: str, calls: int) -> str:
    if "query-compute-apps" in command:
        return "1234,python,512,GPU-0\n"
    if "query-gpu" in command:
        return "0,GPU-0,NVIDIA A100,570.00,55,4096,40960,55,120.0,400,40\n"
    if "/proc/stat" in command:
        counters = calls * 10 + 100
        return (
            f"cpu  {counters} 0 0 {counters} 0 0 0 0 0 0\n0.4 0.2 0.1 1/100 9\n"
            "MemTotal: 100 kB\nMemAvailable: 50 kB\n"
        )
    if command.startswith("df"):
        return (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100 40 60 40% /\n"
        )
    if "/proc/net/dev" in command:
        return "  eth0: 10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0\n"
    if "ps -eo" in command:
        return "  1234 alice 1.0 1.0 100 S python python train.py\n"
    if "hostname" in command:
        return 'node01\n5.15.0\nPRETTY_NAME="Test OS"\n12.0 34.0\n4\n'
    return ""


def ws_settings() -> Settings:
    return Settings(
        fast_interval=0.1,
        medium_interval=0.2,
        process_interval=0.3,
        slow_interval=0.4,
        static_interval=600.0,
        fleet_interval_active=0.2,
        fleet_interval_idle=0.4,
        idle_grace_s=0.5,
        client_queue_size=8,
    )


class WsStub:
    """Duck-typed websocket driving TelemetryService.handle_websocket."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.incoming: list[str] = []
        self.accepted = False
        self.remote_closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        self.sent.append(message)

    async def receive_text(self) -> str:
        while True:
            if self.remote_closed:
                raise _Disconnected()
            if self.incoming:
                return self.incoming.pop(0)
            await asyncio.sleep(0.01)


class _Disconnected(Exception):
    pass


class _Factory:
    def __init__(self) -> None:
        self.executors: dict[str, ScriptedExecutor] = {}

    async def __call__(self, server_id: str, server: ServerRecord) -> ScriptedExecutor:
        executor = ScriptedExecutor()
        self.executors[server_id] = executor
        return executor


def frames(ws: WsStub) -> list[dict[str, Any]]:
    return [json.loads(message) for message in ws.sent]


# ---- REST routes (fake service) ---------------------------------------------


class FakeTelemetry:
    def fleet_summary(self) -> FleetSummary:
        return FleetSummary(
            generated_at=datetime.now(UTC),
            servers=[FleetEntry(server_id="srv", display_name="SRV", gpu_busy=1)],
        )

    def snapshot(self, server_id: str) -> ServerSnapshot | None:
        if server_id != "srv":
            return None
        return ServerSnapshot(server_id=server_id, status=ServerStatus.ONLINE)

    def has_server(self, server_id: str) -> bool:
        return server_id == "srv"

    def gpu_history(self, server_id: str, gpu_index: int, metric: str) -> list[HistoryPoint]:
        if metric not in ("utilization", "vram", "temperature", "power"):
            raise ValueError(f"unknown gpu history metric: {metric!r}")
        if gpu_index < 0:
            raise ValueError("gpu index must be non-negative")
        return [HistoryPoint(t=datetime.now(UTC), v=1.5)]


@pytest.fixture()
def rest_client():
    app = FastAPI()
    app.include_router(telemetry_router)
    app.include_router(realtime_router)
    # Runtime is a plain dataclass: any duck-typed telemetry object works.
    set_runtime(Runtime(settings=Settings(), ssh=object(), telemetry=FakeTelemetry()))  # type: ignore[arg-type]
    yield TestClient(app)
    set_runtime(None)


def test_rest_fleet_and_snapshot_happy_paths(rest_client: TestClient) -> None:
    response = rest_client.get("/api/v1/telemetry/fleet")
    assert response.status_code == 200
    body = response.json()
    assert body["servers"][0]["server_id"] == "srv"
    assert body["servers"][0]["gpu_busy"] == 1

    response = rest_client.get("/api/v1/telemetry/servers/srv")
    assert response.status_code == 200
    assert response.json()["status"] == "online"


def test_rest_snapshot_unknown_server_is_404(rest_client: TestClient) -> None:
    assert rest_client.get("/api/v1/telemetry/servers/zzz").status_code == 404


def test_rest_gpu_history_happy_bad_metric_and_unknown_server(rest_client: TestClient) -> None:
    response = rest_client.get("/api/v1/telemetry/servers/srv/gpu-history?gpu=0&metric=utilization")
    assert response.status_code == 200
    assert response.json()[0]["v"] == 1.5

    bad_metric = rest_client.get("/api/v1/telemetry/servers/srv/gpu-history?gpu=0&metric=bogus")
    assert bad_metric.status_code == 400
    assert "metric" in bad_metric.json()["detail"]["message"]

    assert (
        rest_client.get("/api/v1/telemetry/servers/zzz/gpu-history?metric=utilization").status_code
        == 404
    )
    negative = rest_client.get("/api/v1/telemetry/servers/srv/gpu-history?gpu=-1")
    assert negative.status_code == 400


# ---- TelemetryService unit behaviour ----------------------------------------


@pytest.mark.asyncio
async def test_service_selection_validation_and_last_client_clears() -> None:
    factory = _Factory()
    service = TelemetryService(ws_settings(), FakeRegistry([record("a"), record("b")]), factory)
    await service.start()
    try:
        service.note_client_connected()
        service.set_selected("a")
        assert service._scheduler.detail_servers() == {"a"}
        with pytest.raises(ValueError):
            service.set_selected("")
        with pytest.raises(ValueError):
            service.set_selected(123)  # type: ignore[arg-type]
        service.note_client_disconnected()
        assert service._selected is None  # last client out clears selection
        assert service._scheduler.detail_servers() == set()

        service.set_selected("b")
        service.reconcile_servers([record("a")])  # b disappeared from the registry
        assert service._selected is None
        assert service.client_count == 0
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_service_snapshot_history_and_status() -> None:
    factory = _Factory()
    service = TelemetryService(ws_settings(), FakeRegistry([record("srv")]), factory)
    await service.start()
    try:
        assert service.snapshot("zzz") is None
        service.note_client_connected()
        service.set_selected("srv")
        await wait_until(
            lambda: (
                (snapshot := service.snapshot("srv")) is not None
                and snapshot.cpu is not None
                and snapshot.cpu.percent is not None
            ),
            message="service never produced a cpu sample",
        )
        snapshot = service.snapshot("srv")
        assert snapshot is not None
        assert snapshot.status is ServerStatus.ONLINE
        assert service.status_of("srv") is ServerStatus.ONLINE
        points = service.gpu_history("srv", 0, "utilization")
        assert points and points[0].v == 55.0
        with pytest.raises(ValueError):
            service.gpu_history("srv", 0, "bogus")
        with pytest.raises(ValueError):
            service.gpu_history("srv", -1, "utilization")
    finally:
        await service.stop()


# ---- WebSocket protocol (real service, stub socket) --------------------------


@pytest.mark.asyncio
async def test_ws_hello_select_then_snapshot_frame() -> None:
    factory = _Factory()
    service = TelemetryService(ws_settings(), FakeRegistry([record("srv")]), factory)
    await service.start()
    ws = WsStub()
    task = asyncio.create_task(service.handle_websocket(ws))
    try:
        await wait_until(lambda: any(f.get("type") == "hello" for f in frames(ws)))
        hello = next(f for f in frames(ws) if f["type"] == "hello")
        assert hello["server_ids"] == ["srv"]

        ws.incoming.append(json.dumps({"type": "select", "server_id": "srv"}))
        await wait_until(
            lambda: any(
                frame.get("type") == "server"
                and (frame["snapshot"]["cpu"] or {}).get("percent") is not None
                for frame in frames(ws)
            ),
            message="select intent did not produce a snapshot frame",
        )
        server_frame = next(f for f in frames(ws) if f["type"] == "server")
        assert server_frame["server_id"] == "srv"
        assert server_frame["snapshot"]["gpus"][0]["utilization_percent"] == 55.0

        # Non-select frames are ignored entirely.
        before = len(ws.sent)
        ws.incoming.append(json.dumps({"type": "ping"}))
        ws.incoming.append("not json")
        ws.incoming.append(json.dumps({"type": "select", "server_id": 5}))
        await asyncio.sleep(0.2)
        assert len(frames(ws)) >= before  # session stays alive
    finally:
        ws.remote_closed = True
        with pytest.raises(_Disconnected):
            await asyncio.wait_for(task, timeout=5.0)
        await service.stop()
    assert service.client_count == 0  # cleanup ran on disconnect


@pytest.mark.asyncio
async def test_ws_oversized_frames_are_ignored() -> None:
    factory = _Factory()
    service = TelemetryService(
        ws_settings(), FakeRegistry([record("srv"), record("other")]), factory
    )
    await service.start()
    ws = WsStub()
    task = asyncio.create_task(service.handle_websocket(ws))
    try:
        await wait_until(lambda: ws.accepted)
        oversized = json.dumps({"type": "select", "server_id": "other", "pad": "x" * 5000})
        assert len(oversized.encode()) > 4096
        ws.incoming.append(oversized)
        await asyncio.sleep(0.2)
        assert service._selected is None  # oversized intent dropped

        ws.incoming.append(json.dumps({"type": "select", "server_id": "srv"}))
        await wait_until(
            lambda: any(f.get("type") == "server" for f in frames(ws)),
            message="valid select after oversized frame was lost",
        )
    finally:
        ws.remote_closed = True
        with pytest.raises(_Disconnected):
            await asyncio.wait_for(task, timeout=5.0)
        await service.stop()


# ---- WebSocket route over HTTP (origin policy, real service) -----------------


def realtime_app() -> FastAPI:
    factory = _Factory()
    service = TelemetryService(ws_settings(), FakeRegistry([record("srv")]), factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The routes resolve their collaborator via get_runtime().
        set_runtime(Runtime(settings=ws_settings(), ssh=object(), telemetry=service))  # type: ignore[arg-type]
        await service.start()
        yield
        await service.stop()
        set_runtime(None)

    app = FastAPI(lifespan=lifespan)
    app.include_router(realtime_router)
    app.include_router(telemetry_router)
    return app


def test_ws_route_allows_local_origins_and_missing_origin() -> None:
    with TestClient(realtime_app()) as client:
        for headers in ({"origin": "http://localhost:5173"}, {"origin": "https://127.0.0.1:8420"}):
            with client.websocket_connect("/api/v1/realtime", headers=headers) as websocket:
                assert websocket.receive_json()["type"] == "hello"
        # No Origin header at all (native clients / local tests) is allowed.
        with client.websocket_connect("/api/v1/realtime") as websocket:
            assert websocket.receive_json()["type"] == "hello"


def test_ws_route_rejects_external_origin() -> None:
    with TestClient(realtime_app()) as client:
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/api/v1/realtime", headers={"origin": "https://evil.example"}
            ),
        ):
            pass
        assert rejected.value.code == 1008


def test_ws_route_select_reaches_collection_pipeline() -> None:
    with (
        TestClient(realtime_app()) as client,
        client.websocket_connect("/api/v1/realtime") as websocket,
    ):
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_text(json.dumps({"type": "select", "server_id": "srv"}))
        deadline = time.monotonic() + 5.0
        cpu: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            body = client.get("/api/v1/telemetry/servers/srv").json()
            cpu = body.get("cpu") or {}
            if cpu.get("percent") is not None:
                break
            time.sleep(0.05)
        assert cpu is not None and cpu.get("percent") is not None

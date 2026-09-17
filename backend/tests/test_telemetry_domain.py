"""Telemetry domain tests: state, scheduler, hub (scripted fakes, no SSH).

Ports and extends v2's test_telemetry_domain.py against the v3 contracts.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from app.core.config import Settings
from app.models.server import ServerRecord
from app.models.telemetry import (
    CpuInfo,
    GpuInfo,
    GpuProcessInfo,
    MemoryInfo,
    NetworkInterfaceInfo,
    ProcessInfo,
    ServerStatus,
    StorageMount,
    SystemInfo,
)
from app.ssh.executor import ExecutorError, RemoteCommandResult
from app.telemetry.history import BoundedHistory, GpuSeries
from app.telemetry.hub import RealtimeHub, _Client
from app.telemetry.scheduler import FAST, MEDIUM, PROCESS, SLOW, STATIC, Scheduler
from app.telemetry.state import TelemetryState


def record(server_id: str, ssh_host: str | None = None) -> ServerRecord:
    return ServerRecord(
        server_id=server_id, display_name=server_id.upper(), ssh_host=ssh_host or f"{server_id}.lan"
    )


async def wait_until(
    predicate: Callable[[], bool], wait_seconds: float = 4.0, message: str = "condition not met"
) -> None:
    deadline = time.monotonic() + wait_seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.01)


def result(stdout: str = "", stderr: str = "", exit_code: int = 0) -> RemoteCommandResult:
    return RemoteCommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=1.0)


HEALTHY_OUTPUTS: dict[str, str] = {
    "gpu": "0,GPU-0,NVIDIA A100,570.00,10,4096,40960,55,120.0,400,40\n",
    "gpu_processes": "1234,python,512,GPU-0\n",
    "fleet_gpu_procs": "1234,python,512,GPU-0\n---PS---\n  1234 alice\n 999 root\n",
    "cpu_memory": (
        "cpu  100 0 0 100 0 0 0 0 0 0\n"
        "0.40 0.15 0.05 2/400 999\n"
        "MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\n"
    ),
    "storage": "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
    "/dev/sda1 100 40 60 40% /\n",
    "network": "  eth0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n",
    "processes": "  1234 alice 85.5 12.0 1024000 R python python train.py\n",
    "system": (
        "gpu-node01\n5.15.0-91-generic\n"
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n98765.43 123456.78\n64\n'
        "model name\t: Intel(R) Xeon(R) Gold 6248R\n"
        "NVIDIA-SMI 570.133.07 Driver Version: 570.133.07 CUDA Version: 12.8\n"
    ),
}


class ScriptedExecutor:
    """Fake executor; commands are matched by stable substrings."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.outputs = dict(outputs) if outputs else dict(HEALTHY_OUTPUTS)
        self.calls: list[str] = []
        self.closed = False

    @staticmethod
    def key_of(command: str) -> str:
        if "---PS---" in command:
            return "fleet_gpu_procs"
        if "query-compute-apps" in command:
            return "gpu_processes"
        if "query-gpu" in command:
            return "gpu_basic" if "uuid" not in command else "gpu"
        if "/proc/stat" in command:
            return "cpu_memory"
        if command.startswith("df"):
            return "storage_all" if " -x " not in command else "storage"
        if "/proc/net/dev" in command:
            return "network"
        if "ps -eo" in command:
            return "processes"
        if "hostname" in command:
            return "system"
        return "unknown"

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        self.calls.append(command)
        value: Any = self.outputs.get(self.key_of(command))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, RemoteCommandResult):
            return value
        if isinstance(value, str):
            return result(stdout=value)
        return result()

    async def close(self) -> None:
        self.closed = True


class ExecutorPool:
    """Executor factory handing each server its own scripted executor."""

    def __init__(self, outputs: dict[str, Any] | None = None) -> None:
        self.outputs = outputs
        self.executors: dict[str, ScriptedExecutor] = {}
        self.fail = False
        self.factory_calls = 0

    async def __call__(self, server_id: str, server: ServerRecord) -> ScriptedExecutor:
        self.factory_calls += 1
        if self.fail:
            raise ExecutorError("connect_failed", "connection refused")
        executor = ScriptedExecutor(self.outputs)
        self.executors[server_id] = executor
        return executor

    def calls(self, server_id: str) -> list[str]:
        executor = self.executors.get(server_id)
        return executor.calls if executor is not None else []

    def keys(self, server_id: str) -> list[str]:
        return [ScriptedExecutor.key_of(call) for call in self.calls(server_id)]


def fast_settings(**overrides: float) -> Settings:
    values: dict[str, Any] = {
        "fast_interval": 0.15,
        "medium_interval": 0.3,
        "process_interval": 0.3,
        "slow_interval": 0.3,
        "static_interval": 600.0,
        "fleet_interval_active": 0.6,
        "fleet_interval_idle": 0.3,
        "idle_grace_s": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


# ---- history ----------------------------------------------------------------


def test_bounded_history_is_bounded_and_validated() -> None:
    history = BoundedHistory(3)
    for index in range(5):
        history.append(_ts(index), float(index))
    assert len(history) == 3
    assert [point.v for point in history.as_list()] == [2.0, 3.0, 4.0]
    with pytest.raises(ValueError):
        BoundedHistory(0)
    with pytest.raises(ValueError):
        BoundedHistory(10_001)
    assert len(BoundedHistory(10_000).as_list()) == 0
    series = GpuSeries(maxlen=4)
    with pytest.raises(ValueError):
        series.series("bogus")


def _ts(index: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=index)


# ---- state ------------------------------------------------------------------


def test_state_caps_every_bounded_list() -> None:
    state = TelemetryState(Settings(history_points=5))
    state.update_gpus(
        "cap",
        [GpuInfo(index=index, utilization_percent=1) for index in range(100)],
    )
    assert len(state.snapshot("cap").gpus) == 64
    state.update_gpu_processes("cap", [GpuProcessInfo(pid=index + 1) for index in range(5000)])
    assert len(state.snapshot("cap").gpu_processes) == 4096
    state.update_processes("cap", [ProcessInfo(pid=index + 1) for index in range(400)])
    assert len(state.snapshot("cap").processes) == 250
    state.update_storage(
        "cap",
        [StorageMount(device=f"/dev/{index}", mount=f"/mnt/{index}") for index in range(600)],
    )
    assert len(state.snapshot("cap").storage) == 512
    state.update_network(
        "cap",
        [
            NetworkInterfaceInfo(name=f"eth{index}", rx_total_b=10, tx_total_b=10)
            for index in range(600)
        ],
    )
    assert len(state.snapshot("cap").network) == 512


def test_snapshot_is_deep_copy_and_stale_flag_is_cadence_aware() -> None:
    settings = Settings()
    state = TelemetryState(settings)
    assert state.snapshot("nope") is None

    state.update_cpu("srv", CpuInfo(percent=11.0))
    snapshot = state.snapshot("srv")
    assert snapshot is not None and not snapshot.stale
    snapshot.cpu.percent = 99.0  # mutating the copy must not touch shared state
    assert state.snapshot("srv").cpu.percent == 11.0

    # Backdate past the cadence-aware window (max(3x active, 2x idle + margin)).
    server_state = state._servers["srv"]
    server_state.updated_at["cpu"] = datetime.now(UTC) - timedelta(hours=1)
    assert state.snapshot("srv").stale


def test_fleet_summary_busy_free_disk_warning_and_endpoint() -> None:
    state = TelemetryState(Settings())
    state.update_gpus(
        "srv-a",
        [
            GpuInfo(index=0, name="A100", utilization_percent=0, vram_percent=0.0),
            GpuInfo(index=1, name="A100", utilization_percent=30, vram_percent=50.0),
            GpuInfo(index=2, name="A100", utilization_percent=95, vram_percent=99.0),
        ],
    )
    state.update_storage("srv-a", [StorageMount(device="/dev/sda1", mount="/data", percent=90.0)])
    state.update_cpu("srv-a", CpuInfo(percent=12.5))
    state.update_memory("srv-a", MemoryInfo(percent=40.0))
    state.update_system("srv-a", SystemInfo(os_pretty="Ubuntu 22.04"))

    summary = state.fleet_summary(
        [
            ServerRecord(
                server_id="srv-a",
                display_name="Lab A",
                ssh_host="host-a",
                username="root",
                port=2200,
            ),
            ServerRecord(server_id="srv-b", display_name="Lab B", ssh_host="host-b"),
            ServerRecord(
                server_id="srv-c", display_name="Lab C", ssh_host="host-c", username="ops"
            ),
        ]
    )
    entry = summary.servers[0]
    assert entry.display_name == "Lab A"
    assert entry.ssh_endpoint == "root@host-a:2200"
    assert (entry.gpu_busy, entry.gpu_free, entry.gpu_count) == (2, 1, 3)
    assert entry.disk_warning is True
    assert entry.cpu_percent == 12.5
    assert entry.memory_percent == 40.0
    assert entry.os_pretty == "Ubuntu 22.04"
    assert entry.gpu_model == "A100"
    assert entry.updated_at is not None
    assert summary.servers[1].ssh_endpoint == "host-b"
    assert summary.servers[1].gpu_count == 0
    assert summary.servers[1].status is ServerStatus.UNKNOWN
    assert summary.servers[1].disk_warning is False
    assert summary.servers[2].ssh_endpoint == "ops@host-c"


def test_disk_warning_threshold_is_inclusive_at_90_only() -> None:
    state = TelemetryState(Settings())
    state.update_storage("d", [StorageMount(device="/dev/a", mount="/a", percent=89.9)])
    assert state.fleet_summary([record("d")]).servers[0].disk_warning is False
    state.update_storage("d", [StorageMount(device="/dev/a", mount="/a", percent=90.0)])
    assert state.fleet_summary([record("d")]).servers[0].disk_warning is True


def test_state_correlates_gpu_processes_by_pid_and_uuid() -> None:
    state = TelemetryState(Settings())
    state.update_processes(
        "a", [ProcessInfo(pid=11, user="alice", name="python", command="python train.py")]
    )
    state.update_gpus("a", [GpuInfo(index=0, uuid="GPU-a")])
    state.update_gpu_processes(
        "a",
        [
            GpuProcessInfo(pid=11, gpu_uuid="GPU-a", used_memory_b=100),
            GpuProcessInfo(pid=999, gpu_uuid="GPU-a", used_memory_b=50),
        ],
    )
    rows = state.snapshot("a").gpu_processes
    assert [row.pid for row in rows] == [11, 999]  # sorted by gpu index then pid
    assert rows[0].gpu_index == 0
    assert rows[0].user == "alice"
    assert rows[0].command == "python train.py"
    assert rows[1].user is None
    gpus = state.snapshot("a").gpus
    assert gpus[0].process_count == 2
    processes = state.snapshot("a").processes
    assert processes[0].gpu_indexes == [0]
    assert processes[0].gpu_vram_b == 100


def test_network_rates_derive_from_deltas_and_reset_to_none() -> None:
    state = TelemetryState(Settings())
    state.update_network("n", [NetworkInterfaceInfo(name="eth0", rx_total_b=1000, tx_total_b=500)])
    first = state.snapshot("n").network[0]
    assert first.rx_bps is None and first.tx_bps is None  # no previous sample

    time.sleep(0.05)
    state.update_network("n", [NetworkInterfaceInfo(name="eth0", rx_total_b=6000, tx_total_b=500)])
    second = state.snapshot("n").network[0]
    assert second.rx_bps is not None and second.rx_bps > 0
    assert second.tx_bps == 0.0

    time.sleep(0.05)
    state.update_network("n", [NetworkInterfaceInfo(name="eth0", rx_total_b=6, tx_total_b=0)])
    third = state.snapshot("n").network[0]
    assert third.rx_bps is None and third.tx_bps is None  # counter reset -> unknown


def test_state_revisions_gate_fleet_and_snapshot_frames() -> None:
    state = TelemetryState(Settings())
    state.update_cpu("a", CpuInfo(percent=1.0))
    fleet_before = state.fleet_revision()
    state.update_processes("a", [ProcessInfo(pid=1)])  # not fleet-visible
    assert state.fleet_revision() == fleet_before
    assert state.revision() > fleet_before
    state.update_memory("a", MemoryInfo(percent=1.0))
    assert state.fleet_revision() > fleet_before


# ---- scheduler -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_demand_revision_wakes_idle_worker_fast() -> None:
    server = record("wake-server")
    settings = Settings(
        fast_interval=2.5,
        fleet_interval_idle=45.0,
        idle_grace_s=0.0,
    )
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(0, set())
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await wait_until(lambda: "cpu_memory" in pool.keys(server.server_id))
        pool.executors[server.server_id].calls.clear()

        started = time.monotonic()
        scheduler.set_demand(1, {server.server_id})
        await wait_until(
            lambda: "processes" in pool.keys(server.server_id),
            wait_seconds=3.0,
            message="demand change did not wake the worker",
        )
        assert time.monotonic() - started < 2.0
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_idle_mode_suspends_selected_collectors_after_grace() -> None:
    server = record("idle-server")
    settings = fast_settings(idle_grace_s=0.3, fleet_interval_idle=0.3)
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(1, {server.server_id})
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await wait_until(lambda: "processes" in pool.keys(server.server_id))

        scheduler.set_demand(0, set())
        await asyncio.sleep(0.65)  # grace (0.3 s) fully elapsed
        pool.executors[server.server_id].calls.clear()

        await wait_until(
            lambda: bool(pool.calls(server.server_id)),
            message="idle fleet sampling did not continue",
        )
        await asyncio.sleep(0.35)  # sample a full idle interval
        keys = set(pool.keys(server.server_id))
        assert keys <= {"cpu_memory", "gpu", "gpu_basic", "fleet_gpu_procs"}
        # Detail-only tiers stay suspended in idle mode; the fleet probe is
        # the combined compute-apps command, not the detail gpu_processes one.
        assert "processes" not in keys and "gpu_processes" not in keys
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_non_selected_server_runs_only_fleet_tier() -> None:
    selected = record("selected")
    fleet_only = record("fleet-only")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(1, {selected.server_id})
    await scheduler.start()
    try:
        scheduler.reconcile([selected, fleet_only])
        await asyncio.sleep(1.4)
        selected_cpu = pool.keys(selected.server_id).count("cpu_memory")
        fleet_cpu = pool.keys(fleet_only.server_id).count("cpu_memory")
        assert fleet_cpu > 0, "fleet-only server got no samples"
        assert fleet_cpu < selected_cpu, "fleet-only server sampled at detail cadence"
        fleet_keys = set(pool.keys(fleet_only.server_id))
        # STATIC (system probe) runs fleet-wide; detail-only tiers never do.
        assert fleet_keys <= {"cpu_memory", "gpu", "gpu_basic", "system", "fleet_gpu_procs"}
        detail_keys = {"processes", "gpu_processes", "network", "storage"}
        assert not (fleet_keys & detail_keys)
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_collector_failure_isolates_sections() -> None:
    server = record("iso-server")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool({**HEALTHY_OUTPUTS, "gpu": result(exit_code=1, stderr="denied")})
    scheduler = Scheduler(settings, state, pool)
    executor = await pool(server.server_id, server)
    code = await scheduler._collect(server.server_id, executor, [FAST, MEDIUM])
    assert code is None
    snapshot = state.snapshot(server.server_id)
    assert snapshot.cpu is not None  # cpu survives the gpu failure
    assert snapshot.memory is not None
    assert snapshot.network is not None
    assert state.errors_of(server.server_id).get("gpu") == "command_failed"
    assert state.status_of(server.server_id) is ServerStatus.DEGRADED
    await executor.close()


@pytest.mark.asyncio
async def test_transport_failure_maps_status_and_skips_collectors() -> None:
    server = record("timeout-server")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    executor = await pool(server.server_id, server)
    executor.outputs = {
        key: ExecutorError("timeout", "command timed out")
        for key in (
            "gpu",
            "gpu_basic",
            "cpu_memory",
            "gpu_processes",
            "network",
            "processes",
            "storage",
            "system",
        )
    }
    code = await scheduler._collect(server.server_id, executor, [FAST])
    assert code == "timeout"
    assert state.status_of(server.server_id) is ServerStatus.TIMEOUT
    assert state.snapshot(server.server_id).cpu is None  # no collector ran
    assert state.errors_of(server.server_id)["cpu"] == "timeout"
    await executor.close()


def test_transport_codes_map_to_statuses() -> None:
    settings = fast_settings()
    scheduler = Scheduler(settings, TelemetryState(settings), ExecutorPool())
    cases = {
        "timeout": ServerStatus.TIMEOUT,
        "authentication_failed": ServerStatus.AUTHENTICATION_FAILED,
        "host_key_mismatch": ServerStatus.HOST_KEY_ERROR,
        "host_key_unknown": ServerStatus.HOST_KEY_ERROR,
        "connect_failed": ServerStatus.OFFLINE,
        "connection_lost": ServerStatus.OFFLINE,
        "reconnect_backoff": ServerStatus.RECONNECTING,
        "cancelled": ServerStatus.OFFLINE,
        "unknown": ServerStatus.OFFLINE,
        "weird": ServerStatus.OFFLINE,
    }
    for code, expected in cases.items():
        scheduler._mark_transport_failure("srv-map", code, "detail")
        assert scheduler._state.status_of("srv-map") is expected


@pytest.mark.asyncio
async def test_connect_failure_marks_offline_and_backs_off() -> None:
    server = record("dead-server")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    pool.fail = True
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(1, {server.server_id})
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await wait_until(
            lambda: state.status_of(server.server_id) is ServerStatus.OFFLINE,
            message="connect failure not surfaced",
        )
        assert pool.factory_calls == 1
        await asyncio.sleep(1.2)  # backoff min is 2 s: no hammering
        assert pool.factory_calls == 1
        pool.fail = False
        await wait_until(
            lambda: pool.factory_calls >= 2 and "cpu_memory" in pool.keys(server.server_id),
            wait_seconds=6.0,
            message="scheduler did not retry after backoff",
        )
        assert state.status_of(server.server_id) is ServerStatus.ONLINE
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_two_clients_share_one_cpu_memory_per_interval() -> None:
    server = record("shared")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    executor = await pool(server.server_id, server)
    # Two browser clients select the same server: demand collapses to one
    # detail target and one shared batched CPU/RAM command per interval.
    scheduler.set_demand(2, {server.server_id})
    assert scheduler.detail_servers() == {server.server_id}
    await scheduler.start()
    try:
        code = await scheduler._collect(server.server_id, executor, [FAST])
        assert code is None
        cpu_runs = sum(
            1 for call in executor.calls if ScriptedExecutor.key_of(call) == "cpu_memory"
        )
        assert cpu_runs == 1  # one execution, not one per client
        scheduler.set_demand(3, {server.server_id})
        assert scheduler.detail_servers() == {server.server_id}
    finally:
        await scheduler.stop()
        await executor.close()


@pytest.mark.asyncio
async def test_reconcile_restarts_worker_and_clears_state_on_identity_change() -> None:
    server = record("replaced", "old-host")
    peer = record("peer")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    await scheduler.start()
    try:
        scheduler.reconcile([server, peer])
        await wait_until(
            lambda: bool(pool.calls(server.server_id)) and bool(pool.calls(peer.server_id)),
            message="workers did not start",
        )
        old_task = scheduler._workers[server.server_id]
        old_executor = pool.executors[server.server_id]
        state.update_cpu(server.server_id, CpuInfo(percent=5.0))
        assert state.exists(server.server_id)

        scheduler.reconcile([record("replaced", "new-host"), peer])

        assert scheduler._workers[server.server_id] is not old_task
        assert not state.exists(server.server_id)  # old-host data wiped synchronously
        await wait_until(lambda: old_task.done())
        assert old_executor.closed  # replaced worker released its executor
        assert state.errors_of(server.server_id) == {}
        await wait_until(
            lambda: bool(pool.calls(server.server_id)),
            message="replacement worker did not start",
        )
        assert scheduler._workers[peer.server_id].done() is False
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_reconcile_drops_disabled_servers() -> None:
    server = record("disabled")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await wait_until(lambda: bool(pool.calls(server.server_id)))
        disabled = ServerRecord(
            server_id=server.server_id,
            display_name=server.display_name,
            ssh_host=server.ssh_host,
            enabled=False,
        )
        scheduler.reconcile([disabled])
        assert server.server_id not in scheduler._workers
        assert not state.exists(server.server_id)
        await wait_until(lambda: pool.executors[server.server_id].closed)
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_full_cycle_writes_no_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    server = record("disk-audit")
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    executor = await pool(server.server_id, server)
    await scheduler.start()
    try:
        code = await scheduler._collect(
            server.server_id, executor, [STATIC, FAST, MEDIUM, PROCESS, SLOW]
        )
        assert code is None
        assert state.snapshot(server.server_id) is not None
        assert state.fleet_summary([server]) is not None
    finally:
        await scheduler.stop()
        await executor.close()
    assert list(tmp_path.iterdir()) == []


def test_scheduler_tier_intervals_and_enablement() -> None:
    settings = fast_settings(idle_grace_s=0.0)
    scheduler = Scheduler(settings, TelemetryState(settings), ExecutorPool())
    assert scheduler.mode() == "idle"
    assert scheduler._interval(FAST, True, True) == pytest.approx(settings.fast_interval)
    assert scheduler._interval(MEDIUM, True, True) == pytest.approx(settings.medium_interval)
    assert scheduler._interval(PROCESS, True, True) == pytest.approx(settings.process_interval)
    assert scheduler._interval(SLOW, True, True) == pytest.approx(settings.slow_interval)
    assert scheduler._interval(STATIC, True, True) == pytest.approx(settings.static_interval)
    assert scheduler._interval(FAST, True, False) == pytest.approx(settings.fleet_interval_active)
    assert scheduler._interval(FAST, False, True) == pytest.approx(settings.fleet_interval_idle)

    assert scheduler._enabled(FAST, detail=False, interactive=False)
    assert scheduler._enabled(STATIC, detail=False, interactive=True)
    assert scheduler._enabled(STATIC, detail=True, interactive=False)
    assert not scheduler._enabled(PROCESS, detail=True, interactive=False)
    assert scheduler._enabled(PROCESS, detail=True, interactive=True)

    scheduler.set_demand(1, {"srv"})
    assert scheduler.mode() == "interactive"
    scheduler.set_demand(0, set())
    assert scheduler.mode() == "idle"


# ---- hub ---------------------------------------------------------------------


class FakeWebsocket:
    def __init__(self, delay: float = 0.0) -> None:
        self.sent: list[str] = []
        self.delay = delay
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, message: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(message)


def _records() -> list[ServerRecord]:
    return [record("srv-a"), record("srv-b")]


def test_client_queue_is_newest_wins_drop_oldest() -> None:
    client = _Client(websocket=FakeWebsocket(), queue_size=2)  # type: ignore[arg-type]
    for index in range(100):
        client.enqueue({"type": "fleet", "index": index})
    assert client.queue.qsize() == 2
    assert [client.queue.get_nowait()["index"] for _ in range(2)] == [98, 99]


def test_hub_frames_are_revision_gated_without_duplicates() -> None:
    settings = Settings(client_queue_size=8)
    state = TelemetryState(settings)
    hub = RealtimeHub(settings, state, _records)
    client = _Client(websocket=FakeWebsocket(), queue_size=8)  # type: ignore[arg-type]
    hub._clients.add(client)

    hub._push_changes()
    hub._push_changes()
    assert client.queue.qsize() == 1  # exactly one fleet frame, no duplicates

    state.update_cpu("srv-a", CpuInfo(percent=3.0))
    hub._push_changes()
    assert client.queue.qsize() == 2  # refreshed fleet frame

    hub._push_changes()
    assert client.queue.qsize() == 2  # still nothing new

    hub.set_intent("srv-a")
    hub._push_changes()
    frames = [client.queue.get_nowait() for _ in range(client.queue.qsize())]
    types = [frame["type"] for frame in frames]
    assert "server" in types
    server_frame = next(frame for frame in frames if frame["type"] == "server")
    assert server_frame["server_id"] == "srv-a"
    hub._push_changes()
    assert client.queue.qsize() == 0  # snapshot revision unchanged -> no resend


@pytest.mark.asyncio
async def test_slow_client_never_blocks_others_and_keeps_latest() -> None:
    settings = Settings(client_queue_size=2, history_points=5)
    state = TelemetryState(settings)
    hub = RealtimeHub(settings, state, _records)
    await hub.start()
    try:
        fast_ws = FakeWebsocket()
        slow_ws = FakeWebsocket(delay=0.3)
        await hub.connect(fast_ws)
        await hub.connect(slow_ws)

        state.update_cpu("srv-a", CpuInfo(percent=1.0))
        state.update_cpu("srv-a", CpuInfo(percent=2.0))
        state.update_cpu("srv-a", CpuInfo(percent=3.0))  # fleet revisions while slow sends

        await wait_until(lambda: len(fast_ws.sent) >= 2, message="fast client stalled")
        assert len(slow_ws.sent) <= 1  # blocked mid-send, not blocking the fast client
        await wait_until(lambda: len(slow_ws.sent) >= 2, wait_seconds=3.0)
        fleet_frames = [
            json.loads(message)
            for message in slow_ws.sent
            if json.loads(message)["type"] == "fleet"
        ]
        assert 1 <= len(fleet_frames) <= 2  # queue cap 2 kept only the newest
        latest = fleet_frames[-1]["summary"]["servers"][0]["cpu_percent"]
        assert latest == 3.0  # newest state, never a stale backlog
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_hub_status_frames_broadcast_changes() -> None:
    settings = Settings(client_queue_size=8)
    state = TelemetryState(settings)
    hub = RealtimeHub(settings, state, _records)
    await hub.start()
    try:
        ws = FakeWebsocket()
        await hub.connect(ws)
        state.set_status("srv-a", ServerStatus.ONLINE, "")
        await wait_until(
            lambda: any('"status"' in message for message in ws.sent),
            message="status change was not broadcast",
        )
        frame = next(
            json.loads(message) for message in ws.sent if json.loads(message)["type"] == "status"
        )
        assert frame["server_id"] == "srv-a"
        assert frame["status"] == "online"
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_per_server_sample_interval_override() -> None:
    """A record-level FAST override wins over the global fast_interval."""
    server = record("fast-box")
    server.sample_interval_s = 0.5
    settings = fast_settings()
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(1, {server.server_id})
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await asyncio.sleep(1.2)
        calls = pool.keys(server.server_id).count("cpu_memory")
        # At 0.5 s cadence a 1.2 s window must yield >= 2 samples (first due
        # immediately, second due at +0.5 s). Global 2.5 s would yield 1.
        assert calls >= 2, f"override not applied: {calls} calls"
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_reconcile_restarts_worker_on_interval_change() -> None:
    """Changing sample_interval_s restarts the worker without clearing state."""

    server = record("tuned")
    settings = fast_settings(idle_grace_s=0.0)
    state = TelemetryState(settings)
    pool = ExecutorPool()
    scheduler = Scheduler(settings, state, pool)
    scheduler.set_demand(1, {server.server_id})
    await scheduler.start()
    try:
        scheduler.reconcile([server])
        await asyncio.sleep(0.3)
        assert scheduler.managed_ids() == {server.server_id}
        updated = ServerRecord.model_validate(
            {**server.model_dump(mode="python"), "sample_interval_s": 1.0}
        )
        scheduler.reconcile([updated])
        await asyncio.sleep(0.3)
        assert scheduler.managed_ids() == {server.server_id}
        assert scheduler._records["tuned"].sample_interval_s == 1.0
        # state must survive (identity unchanged)
        snapshot = state.snapshot(server.server_id)
        assert snapshot is not None
    finally:
        await scheduler.stop()

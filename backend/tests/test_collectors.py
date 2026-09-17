"""Collector parser + behaviour tests (scripted fakes, no SSH transport)."""

from __future__ import annotations

import pytest
from app.collectors.base import CollectorError
from app.collectors.cpu import CpuCollector, parse_proc_stat
from app.collectors.gpu import (
    GpuCollector,
    GpuProcessCollector,
    parse_gpu_processes,
    parse_gpu_query,
)
from app.collectors.memory import MemoryCollector, parse_meminfo
from app.collectors.network import NetworkCollector, parse_proc_net_dev
from app.collectors.processes import ProcessCollector, parse_ps
from app.collectors.storage import StorageCollector, parse_df
from app.collectors.system_info import SystemInfoCollector, parse_system_info
from app.models.telemetry import GpuAvailability
from app.ssh.executor import ExecutorError, RemoteCommandResult


def result(stdout: str = "", stderr: str = "", exit_code: int = 0) -> RemoteCommandResult:
    return RemoteCommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=1.0)


class ScriptedExecutor:
    """Fake executor: matches fixed commands by stable substrings and can
    serve queued outputs (a list is consumed front-first, then repeats)."""

    def __init__(self, outputs: dict[str, object] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[str] = []
        self.closed = False

    @staticmethod
    def key_of(command: str) -> str:
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

    def _next(self, key: str) -> RemoteCommandResult | Exception | None:
        value = self.outputs.get(key)
        if isinstance(value, list):
            if not value:
                return None
            return value.pop(0) if len(value) > 1 else value[0]
        return value  # type: ignore[return-value]

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        self.calls.append(command)
        value = self._next(self.key_of(command))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, RemoteCommandResult):
            return value
        if isinstance(value, str):
            return result(stdout=value)
        return result()

    async def close(self) -> None:
        self.closed = True


# ---- GPU --------------------------------------------------------------------


def test_gpu_query_parses_multi_gpu_na_and_partial_rows() -> None:
    text = (
        "0,GPU-a,NVIDIA A100,570.00,98,24564,40960,70,310.0,400,N/A\n"
        "1,GPU-b,NVIDIA A100,570.00,12,1024,40960,N/A,N/A,N/A,N/A\n"
        "malformed row\n"
        "2,GPU-c,NVIDIA A100,570.00,0,0,40960\n"
    )
    gpus = parse_gpu_query(text)
    assert [gpu.index for gpu in gpus] == [0, 1, 2]
    assert gpus[0].vram_total_b == 40960 * 1024 * 1024
    assert gpus[0].vram_used_b == 24564 * 1024 * 1024
    assert gpus[0].availability is GpuAvailability.SATURATED
    assert gpus[1].temperature_c is None
    assert gpus[1].power_watts is None
    assert gpus[1].availability is GpuAvailability.ACTIVE  # util 12 > 5
    assert gpus[2].vram_percent == 0.0
    assert gpus[2].availability is GpuAvailability.FREE
    assert gpus[2].temperature_c is None  # trailing fields may be absent
    assert parse_gpu_query("") == []


def test_gpu_query_handles_quoted_names_and_vram_saturation() -> None:
    text = (
        '0,GPU-a,"Quadro RTX 6000, rev A",570.00,0,40000,40960,40,50,250,30\n'
        "1,GPU-b,NVIDIA A100,570.00,0,0,40960,40,50,250,30\n"
    )
    gpus = parse_gpu_query(text)
    assert gpus[0].name == "Quadro RTX 6000, rev A"
    # 40000/40960 >= 95% saturates even at zero utilization.
    assert gpus[0].availability is GpuAvailability.SATURATED
    assert gpus[1].availability is GpuAvailability.FREE


def test_gpu_query_basic_layout() -> None:
    text = "0,NVIDIA A100,55,2048,40960,61\n"
    gpus = parse_gpu_query(text, basic=True)
    assert len(gpus) == 1
    gpu = gpus[0]
    assert gpu.index == 0
    assert gpu.name == "NVIDIA A100"
    assert gpu.uuid is None
    assert gpu.utilization_percent == 55.0
    assert gpu.vram_used_b == 2048 * 1024 * 1024
    assert gpu.vram_total_b == 40960 * 1024 * 1024
    assert gpu.temperature_c == 61.0
    assert gpu.power_watts is None
    assert gpu.availability is GpuAvailability.ACTIVE


@pytest.mark.asyncio
async def test_gpu_collector_falls_back_to_basic_once() -> None:
    executor = ScriptedExecutor(
        {
            "gpu": result(
                exit_code=1,
                stderr='NVIDIA-SMI has failed because "Invalid query" field fan.speed',
            ),
            "gpu_basic": "0,NVIDIA A100,44,1024,40960,55\n",
        }
    )
    collector = GpuCollector()
    gpus = await collector.collect(executor)
    assert [gpu.index for gpu in gpus] == [0]
    assert gpus[0].uuid is None
    assert executor.key_of(executor.calls[0]) == "gpu"
    assert executor.key_of(executor.calls[1]) == "gpu_basic"

    # The fallback is remembered: the next sample runs only the basic query.
    await collector.collect(executor)
    assert executor.key_of(executor.calls[2]) == "gpu_basic"
    assert len(executor.calls) == 3


@pytest.mark.asyncio
async def test_gpu_collector_missing_binary_is_unavailable_not_crash() -> None:
    for outcome in (
        result(exit_code=127, stderr="bash: nvidia-smi: command not found"),
        result(exit_code=6, stderr="NVIDIA-SMI has failed because No devices were found"),
        result(exit_code=1, stderr="NVML: driver/library version mismatch"),
        result(exit_code=1, stderr="Failed to initialize NVML"),
    ):
        with pytest.raises(CollectorError) as caught:
            await GpuCollector().collect(ScriptedExecutor({"gpu": outcome}))
        assert caught.value.code == "unavailable"


@pytest.mark.asyncio
async def test_gpu_collector_maps_transport_error_code() -> None:
    executor = ScriptedExecutor({"gpu": ExecutorError("timeout", "command timed out")})
    with pytest.raises(CollectorError) as caught:
        await GpuCollector().collect(executor)
    assert caught.value.code == "timeout"
    assert caught.value.detail == "command timed out"


@pytest.mark.asyncio
async def test_gpu_collector_accepts_prefetched_result() -> None:
    executor = ScriptedExecutor()
    gpus = await GpuCollector().collect(executor, result("0,GPU-x,A100,570,7,1,40960,40\n"))
    assert gpus[0].utilization_percent == 7.0
    assert executor.calls == []  # no extra round trip


def test_gpu_processes_parses_all_row_shapes() -> None:
    text = (
        "1234,python,512,GPU-uuid-0\n"
        "0,junk,10,GPU-uuid-1\n"
        "-1,nope,5,GPU-uuid-2\n"
        "77,nvidia-smi,GPU-uuid-3\n"
        "999,python,64,GPU-uuid-0\n"
    )
    processes = parse_gpu_processes(text)
    assert [p.pid for p in processes] == [1234, 77, 999]  # pid < 1 dropped
    assert processes[0].used_memory_b == 512 * 1024 * 1024
    assert processes[0].gpu_uuid == "GPU-uuid-0"
    assert processes[1].process_name is None  # three-field row: no name
    assert processes[1].gpu_uuid == "GPU-uuid-3"
    assert parse_gpu_processes("") == []


@pytest.mark.asyncio
async def test_gpu_process_collector_unavailable_on_missing_binary() -> None:
    executor = ScriptedExecutor(
        {"gpu_processes": result(exit_code=127, stderr="command not found")}
    )
    with pytest.raises(CollectorError) as caught:
        await GpuProcessCollector().collect(executor)
    assert caught.value.code == "unavailable"


# ---- CPU --------------------------------------------------------------------


def test_cpu_first_sample_has_no_percent_and_delta_math() -> None:
    first = "cpu  100 0 0 100 0 0 0 0 0 0\n0.40 0.15 0.05 2/400 999\n"
    (utilization, counters), loads = parse_proc_stat(first, None)
    assert utilization is None
    assert loads == (0.40, 0.15, 0.05)
    total, idle = counters

    second = "cpu  200 0 0 200 0 0 0 0 0 0\n0.40 0.15 0.05 2/400 999\n"
    (utilization, _), _loads = parse_proc_stat(second, (total, idle))
    # idle delta 100 of total delta 200 -> 50% busy
    assert utilization == 50.0


def test_cpu_counter_reset_returns_none() -> None:
    (utilization, _), _loads = parse_proc_stat(
        "cpu  10 0 0 20 10 0 0 0 0 0\n1/2 3\n", previous=(10_000, 5_000)
    )
    assert utilization is None


def test_cpu_missing_aggregate_line_is_parse_error() -> None:
    with pytest.raises(CollectorError):
        parse_proc_stat("cpu0 1 2 3 4 5 6 7 8\n")


@pytest.mark.asyncio
async def test_cpu_collector_keeps_counter_state_between_samples() -> None:
    executor = ScriptedExecutor(
        {
            "cpu_memory": [
                "cpu  100 0 0 100 0 0 0 0 0 0\n0.40 0.15 0.05 2/400 999\n",
                "cpu  200 0 0 200 0 0 0 0 0 0\n0.40 0.15 0.05 2/400 999\n",
            ]
        }
    )
    collector = CpuCollector()
    first = await collector.collect(executor)
    assert first.percent is None
    assert first.load_1 == 0.40
    second = await collector.collect(executor)
    assert second.percent == 50.0


# ---- memory -----------------------------------------------------------------


def test_meminfo_normalizes_units_and_falls_back_to_memfree() -> None:
    info = parse_meminfo("MemTotal: 1024 kB\nMemAvailable: 256 kB\nSwapTotal: 0 kB\n")
    assert info.total_b == 1024 * 1024
    assert info.available_b == 256 * 1024
    assert info.used_b == 768 * 1024
    assert info.percent == 75.0

    fallback = parse_meminfo("MemTotal: 1024 kB\nMemFree: 512 kB\n")
    assert fallback.available_b == 512 * 1024
    assert fallback.percent == 50.0

    empty = parse_meminfo("Broken 123\n")
    assert empty.total_b is None and empty.percent is None


@pytest.mark.asyncio
async def test_memory_collector_uses_shared_command() -> None:
    executor = ScriptedExecutor({"cpu_memory": "MemTotal: 100 kB\nMemAvailable: 25 kB\n"})
    info = await MemoryCollector().collect(executor)
    assert info.percent == 75.0
    assert executor.key_of(executor.calls[0]) == "cpu_memory"


# ---- processes --------------------------------------------------------------


def test_ps_keeps_args_and_does_not_duplicate_comm() -> None:
    text = (
        "  1234 alice 85.5 12.0 1024000 R python python train.py --epochs 90\n"
        "   99 bob 0.0 0.1 4096 S bash\n"
        "garbage\n"
        "  -5 root 0 0 0 Z kthreadd\n"
    )
    processes = parse_ps(text)
    assert [p.pid for p in processes] == [1234, 99]
    first = processes[0]
    assert first.name == "python"
    assert first.command == "python train.py --epochs 90"
    assert first.user == "alice"
    assert first.rss_b == 1024000 * 1024
    assert first.cpu_percent == 85.5
    assert first.state == "R"
    second = processes[1]
    assert second.name == "bash"
    assert second.command is None  # no args column content
    assert parse_ps("bad row") == []


def test_ps_caps_rows_and_truncates() -> None:
    lines = [f"{index} user 1.0 1.0 100 S comm{index} args{index}" for index in range(1, 1301)]
    processes = parse_ps("\n".join(lines))
    # Parser cap feeds the pid->user/command correlation index; the rendered
    # table is capped separately in state (250).
    assert len(processes) == 1200
    long_comm = f"{1} user 1.0 1.0 100 S {'c' * 500} x\n"
    parsed = parse_ps(long_comm)
    assert len(parsed[0].name) == 128


@pytest.mark.asyncio
async def test_process_collector_uses_spec_command() -> None:
    executor = ScriptedExecutor({"processes": "  5 u 1 1 100 S sh sh -c ls\n"})
    processes = await ProcessCollector().collect(executor)
    assert processes[0].command == "sh -c ls"
    assert "ps -eo" in executor.calls[0]


# ---- storage ----------------------------------------------------------------


def test_df_parses_spaces_in_mount_and_ignores_virtual() -> None:
    text = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "/dev/sda1 100 40 60 40% /\n"
        "/dev/sdb1 200 100 100 50% /mnt/my data\n"
        "tmpfs 8 0 8 0% /run\n"
        "/dev/sdc1 300 297 3 99% /var\n"
    )
    mounts = parse_df(text)
    assert [m.mount for m in mounts] == ["/", "/mnt/my data", "/var"]
    assert mounts[1].total_b == 200 * 1024
    assert mounts[1].percent == 50.0
    assert mounts[2].percent == 99.0
    assert parse_df("") == []


@pytest.mark.asyncio
async def test_storage_collector_falls_back_when_df_flags_rejected() -> None:
    executor = ScriptedExecutor(
        {
            "storage": result(exit_code=1, stderr="df: unrecognized option '-x'"),
            "storage_all": "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/sda1 100 40 60 40% /\n",
        }
    )
    collector = StorageCollector()
    mounts = await collector.collect(executor)
    assert [m.mount for m in mounts] == ["/"]
    assert " -x " not in executor.calls[1]  # retried without the filters
    await collector.collect(executor)  # fallback remembered: no -x attempt again
    assert len(executor.calls) == 3
    assert " -x " not in executor.calls[2]


@pytest.mark.asyncio
async def test_storage_collector_fails_when_both_specs_fail() -> None:
    executor = ScriptedExecutor(
        {"storage": result(exit_code=1), "storage_all": result(exit_code=1)}
    )
    with pytest.raises(CollectorError) as caught:
        await StorageCollector().collect(executor)
    assert caught.value.code == "command_failed"


# ---- network ----------------------------------------------------------------


def test_proc_net_dev_reads_rx_and_tx_columns() -> None:
    text = (
        "Inter-|   Receive                        "
        "|  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes "
        "packets errs drop fifo colls carrier compressed\n"
        "    lo: 100 1 0 0 0 0 0 0    100 1 0 0 0 0 0 0\n"
        "  eth0: 1000 10 0 0 0 0 0 0   2000 20 0 0 0 0 0 0\n"
        "broken-line\n"
    )
    interfaces = parse_proc_net_dev(text)
    assert [(i.name, i.rx_total_b, i.tx_total_b) for i in interfaces] == [("eth0", 1000, 2000)]
    assert parse_proc_net_dev("broken") == []


@pytest.mark.asyncio
async def test_network_collector_reads_counters() -> None:
    executor = ScriptedExecutor({"network": "  eth0: 5 0 0 0 0 0 0 0 7 0 0 0 0 0 0 0\n"})
    interfaces = await NetworkCollector().collect(executor)
    assert interfaces[0].rx_total_b == 5
    assert interfaces[0].tx_total_b == 7
    assert interfaces[0].rx_bps is None  # rates are derived in state


# ---- system -----------------------------------------------------------------


def test_system_info_parses_all_probe_lines() -> None:
    text = (
        "gpu-node01\n"
        "5.15.0-91-generic\n"
        'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'
        "98765.43 123456.78\n"
        "64\n"
        "model name\t: Intel(R) Xeon(R) Gold 6248R\n"
        "NVIDIA-SMI 570.133.07 Driver Version: 570.133.07 CUDA Version: 12.8\n"
    )
    info = parse_system_info(text)
    assert info.hostname == "gpu-node01"
    assert info.kernel == "5.15.0-91-generic"
    assert info.os_pretty == "Ubuntu 22.04.4 LTS"
    assert info.uptime_s == 98765.43
    assert info.cores_logical == 64
    assert info.cpu_model == "Intel(R) Xeon(R) Gold 6248R"
    assert info.driver_version == "570.133.07"


def test_system_info_survives_missing_optional_probes() -> None:
    info = parse_system_info("bare-node\n6.8.0-generic\n123.5 45.6\n8\n")
    assert info.os_pretty is None
    assert info.cpu_model is None
    assert info.driver_version is None
    assert info.uptime_s == 123.5
    assert info.cores_logical == 8


@pytest.mark.asyncio
async def test_system_collector_uses_spec_command() -> None:
    executor = ScriptedExecutor({"system": "node\nkernel\n"})
    info = await SystemInfoCollector().collect(executor)
    assert info.hostname == "node"
    assert "hostname" in executor.calls[0]

"""Fixed remote command specifications (Sol-owned contract).

Every remote command in the product is declared here, once, read-only except
the two named kill actions. No user input ever reaches these strings except
through `validate_pid` + `kill_spec`. Keys are the cross-layer vocabulary:
collectors reference them by name; see docs/IMPLEMENTATION_PLAN.md.
"""

from __future__ import annotations

from dataclasses import dataclass

GPU_FIELDS = (
    "index",
    "uuid",
    "name",
    "driver_version",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
    "power.draw",
    "power.limit",
    "fan.speed",
)

# Basic fallback for drivers that reject one of the extended fields.
GPU_BASIC_FIELDS = (
    "index",
    "name",
    "utilization.gpu",
    "memory.used",
    "memory.total",
    "temperature.gpu",
)


def _nvidia_smi(fields: tuple[str, ...]) -> str:
    joined = ",".join(fields)
    return f"nvidia-smi --query-gpu={joined} --format=csv,noheader,nounits"


@dataclass(frozen=True, slots=True)
class CommandSpec:
    key: str
    command: str
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.key.replace("_", "").isalnum():
            raise ValueError(f"invalid command key: {self.key!r}")
        if any(ch in self.command for ch in ("\x00", "\r")):
            raise ValueError(f"control character in command {self.key!r}")


GPU = CommandSpec("gpu", _nvidia_smi(GPU_FIELDS), timeout_s=10.0)
GPU_BASIC = CommandSpec("gpu_basic", _nvidia_smi(GPU_BASIC_FIELDS), timeout_s=10.0)
GPU_PROCESSES = CommandSpec(
    "gpu_processes",
    "nvidia-smi --query-compute-apps=pid,process_name,used_memory,gpu_uuid"
    " --format=csv,noheader,nounits",
    timeout_s=10.0,
)

# One round trip covers CPU counters, load averages and memory info.
CPU_MEMORY = CommandSpec("cpu_memory", "cat /proc/stat /proc/loadavg /proc/meminfo", timeout_s=10.0)
STORAGE = CommandSpec(
    "storage",
    "df -kP -x tmpfs -x devtmpfs -x efivarfs -x squashfs -x overlay",
    timeout_s=15.0,
)
STORAGE_ALL = CommandSpec("storage_all", "df -kP", timeout_s=15.0)
NETWORK = CommandSpec("network", "cat /proc/net/dev", timeout_s=10.0)
# Equal-sign form output: "pid user pcpu pmem rss stat comm args(args may contain spaces)"
PROCESSES = CommandSpec(
    "processes",
    "ps -eo pid=,user:24=,pcpu=,pmem=,rss=,stat=,comm=,args=",
    timeout_s=15.0,
)
# Fleet-tier probe: compute apps + the owning users in ONE round trip.
# `nvidia-smi --query-compute-apps` does not carry the user, so the command
# appends a cheap pid->user listing after a marker line.
FLEET_GPU_PROCS = CommandSpec(
    "fleet_gpu_procs",
    "nvidia-smi --query-compute-apps=pid,process_name,used_memory,gpu_uuid"
    " --format=csv,noheader,nounits;"
    " echo '---PS---';"
    " ps -eo pid=,user:24=",
    timeout_s=12.0,
)

SYSTEM = CommandSpec(
    "system",
    "hostname; uname -r; grep '^PRETTY_NAME' /etc/os-release 2>/dev/null;"
    " cat /proc/uptime; nproc;"
    " grep -m1 'model name' /proc/cpuinfo 2>/dev/null;"
    " nvidia-smi --version 2>/dev/null | grep -i 'driver version' | head -n 1",
    timeout_s=10.0,
)

SPEC_BY_KEY: dict[str, CommandSpec] = {
    spec.key: spec
    for spec in (
        GPU,
        GPU_BASIC,
        GPU_PROCESSES,
        CPU_MEMORY,
        STORAGE,
        STORAGE_ALL,
        NETWORK,
        PROCESSES,
        FLEET_GPU_PROCS,
        SYSTEM,
    )
}


def validate_pid(pid: int) -> int:
    """Reject anything that is not a plain positive PID."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ValueError("pid must be an integer")
    if not 1 <= pid <= 4_194_304:  # default /proc/sys/kernel/pid_max ceiling
        raise ValueError("pid out of range")
    return pid


def kill_spec(signal_name: str, pid: int) -> CommandSpec:
    """Named action command: TERM or KILL against a validated PID. No other signals.

    Output contract: stderr is merged into stdout and terminated by `rc=<n>`,
    which the action service parses for classified outcomes.
    """
    if signal_name not in ("TERM", "KILL"):
        raise ValueError(f"signal not allowlisted: {signal_name!r}")
    validate_pid(pid)
    command = f"kill -{signal_name} {pid} 2>&1; echo rc=$?"
    return CommandSpec(f"kill_{signal_name.lower()}_{pid}", command, timeout_s=8.0)

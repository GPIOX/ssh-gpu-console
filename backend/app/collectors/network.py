"""Network counters from ``/proc/net/dev``; rates are derived in shared state."""

from __future__ import annotations

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import NetworkInterfaceInfo
from app.ssh.commands import NETWORK
from app.ssh.executor import RemoteCommandResult


def parse_proc_net_dev(
    stdout: str, *, include_loopback: bool = False
) -> list[NetworkInterfaceInfo]:
    """Byte counters per interface: receive is field 0, transmit field 8."""

    interfaces: list[NetworkInterfaceInfo] = []
    for line in stdout.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        interface = name.strip()
        if not interface or (interface == "lo" and not include_loopback):
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            rx_bytes, tx_bytes = int(fields[0]), int(fields[8])
        except (TypeError, ValueError):
            continue
        if rx_bytes < 0 or tx_bytes < 0:
            continue
        interfaces.append(
            NetworkInterfaceInfo(name=interface, rx_total_b=rx_bytes, tx_total_b=tx_bytes)
        )
    return interfaces


class NetworkCollector:
    """Interface byte counters at the MEDIUM cadence."""

    name = "network"
    spec = NETWORK

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> list[NetworkInterfaceInfo]:
        if result is None:
            result = await run_spec(executor, self.spec)
        if result.exit_code != 0:
            raise CollectorError(
                "command_failed",
                f"network: /proc/net/dev read failed (exit {result.exit_code}): "
                f"{result.stderr.strip()[:300]}",
            )
        return parse_proc_net_dev(result.stdout)

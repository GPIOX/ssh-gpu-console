"""Slow-changing system metadata collector.

The SYSTEM spec concatenates several one-line probes; most lines are
recognized by pattern so a missing optional probe (os-release, nvidia-smi)
never shifts the meaning of the others. Hostname and kernel are the first
two lines by position — both probes always emit exactly one line.
"""

from __future__ import annotations

import contextlib
import re

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import SystemInfo
from app.ssh.commands import SYSTEM
from app.ssh.executor import RemoteCommandResult

_DRIVER_RE = re.compile(r"driver\s+version\s*:\s*([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE)
_UPTIME_RE = re.compile(r"^\d+\.\d+\s+\d+\.\d+$")


def parse_system_info(stdout: str) -> SystemInfo:
    """Parse the SYSTEM spec output into typed metadata."""

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    hostname = lines[0] if lines else None
    kernel = lines[1] if len(lines) > 1 else None

    os_pretty: str | None = None
    uptime_s: float | None = None
    cores_logical: int | None = None
    cpu_model: str | None = None
    driver_version: str | None = None

    for line in lines[2:]:
        if line.startswith("PRETTY_NAME="):
            os_pretty = line.partition("=")[2].strip().strip('"') or None
        elif _UPTIME_RE.match(line):
            with contextlib.suppress(ValueError):
                uptime_s = float(line.split()[0])
        elif "model name" in line:
            cpu_model = line.partition(":")[2].strip() or None
        elif "driver version" in line.lower():
            match = _DRIVER_RE.search(line)
            driver_version = match.group(1) if match else line[:120]
        elif re.fullmatch(r"\d+", line):
            cores_logical = int(line)
    return SystemInfo(
        hostname=hostname,
        os_pretty=os_pretty,
        kernel=kernel,
        uptime_s=uptime_s,
        cores_logical=cores_logical,
        cpu_model=cpu_model,
        driver_version=driver_version,
    )


class SystemInfoCollector:
    """Static-ish system identity at the STATIC cadence."""

    name = "system"
    spec = SYSTEM

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> SystemInfo:
        if result is None:
            result = await run_spec(executor, self.spec)
        if result.exit_code != 0:
            # The guarded probes keep exit 0 in practice; treat anything else
            # as a failed section rather than fabricating partial identity.
            raise CollectorError(
                "command_failed",
                f"system: probe failed (exit {result.exit_code}): {result.stderr.strip()[:300]}",
            )
        return parse_system_info(result.stdout)

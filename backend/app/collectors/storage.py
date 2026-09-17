"""Slow storage-capacity collector.

``df -x`` filters are unsupported on some ancient/coreutils builds; when the
primary spec fails the collector retries ``df -kP`` once and sticks to it.
"""

from __future__ import annotations

from app.collectors.base import CollectorError, ExecutorLike
from app.collectors.gpu import run_spec
from app.models.telemetry import StorageMount
from app.ssh.commands import STORAGE, STORAGE_ALL, CommandSpec
from app.ssh.executor import RemoteCommandResult

_IGNORED_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/boot/efi")


def parse_df(stdout: str) -> list[StorageMount]:
    """Parse POSIX ``df -kP`` output (with or without a Type column).

    Mount points may contain spaces; everything past the fixed columns is
    the mount path.
    """

    lines = stdout.splitlines()
    if not lines:
        return []
    has_type = "Type" in lines[0].split()
    mounts: list[StorageMount] = []
    for line in lines[1:]:
        fields = line.split()
        if has_type:
            if len(fields) < 7:
                continue
            device, fstype, blocks, used, available, percent = fields[:6]
            mount = " ".join(fields[6:])
        else:
            if len(fields) < 6:
                continue
            device, blocks, used, available, percent = fields[:5]
            fstype = None
            mount = " ".join(fields[5:])
        if not mount.startswith("/") or any(
            mount == ignored or mount.startswith(ignored + "/") for ignored in _IGNORED_MOUNTS
        ):
            continue
        try:
            total_b = int(blocks) * 1024
            used_b = int(used) * 1024
            free_b = int(available) * 1024
            percent_value = float(percent.rstrip("%"))
        except (TypeError, ValueError):
            continue
        if not 0 <= percent_value <= 100:
            continue
        mounts.append(
            StorageMount(
                device=device,
                fstype=fstype or None,
                mount=mount,
                total_b=total_b,
                used_b=used_b,
                free_b=free_b,
                percent=round(percent_value, 1),
            )
        )
    return mounts


class StorageCollector:
    """Mounted filesystem capacity at the SLOW cadence."""

    name = "storage"

    def __init__(self) -> None:
        self._use_all = False

    @property
    def spec(self) -> CommandSpec:
        return STORAGE_ALL if self._use_all else STORAGE

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> list[StorageMount]:
        if not self._use_all:
            outcome = result if result is not None else await run_spec(executor, STORAGE)
            if outcome.exit_code != 0:
                # -x filters rejected: retry once without them, then remember.
                self._use_all = True
                outcome = await run_spec(executor, STORAGE_ALL)
        else:
            outcome = result if result is not None else await run_spec(executor, STORAGE_ALL)
        if outcome.exit_code != 0:
            raise CollectorError(
                "command_failed",
                f"storage: df failed (exit {outcome.exit_code}): {outcome.stderr.strip()[:300]}",
            )
        return parse_df(outcome.stdout)

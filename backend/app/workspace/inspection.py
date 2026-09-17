"""Explicit placement inspection (user-requested, never background).

One SSH round trip per inspection: stat the remote path for existence, type
and size; optionally count top-level files. Results are runtime observations
held in RAM — the previous inspection is replaced on the next request.
"""

from __future__ import annotations

import asyncio
import time

from app.collectors.base import ExecutorLike
from app.core.errors import ConflictError
from app.models.workspace import (
    InspectionState,
    PlacementInspection,
    PlacementRecord,
)


class PlacementInspector:
    """Runs explicit placement checks over the existing SSH executor.

    The instance owns the observation cache: a shared instance means project
    and single-placement inspections see the same latest observations. The
    cache dict is injectable so embedders can seed or share it.
    """

    def __init__(self, cache: dict[str, PlacementInspection] | None = None) -> None:
        self._latest: dict[str, PlacementInspection] = cache if cache is not None else {}
        self._lock = asyncio.Lock()

    def latest(self, placement_id: str) -> PlacementInspection | None:
        return self._latest.get(placement_id)

    def record(self, inspection: PlacementInspection) -> PlacementInspection:
        """Store an externally derived observation (e.g. an unavailable server
        that never reached the executor) in the same cache."""
        return self._record(inspection.placement_id, inspection)

    async def inspect(
        self, executor: ExecutorLike, placement: PlacementRecord
    ) -> PlacementInspection:
        """Check one placement via a fixed, quoted read-only command."""
        path = _safe_path(placement.remote_path)
        command = (
            f"p={path};"
            'if [ -e "$p" ]; then'
            " printf 'EXISTS '; stat -c '%F|%s' -- \"$p\";"
            " if [ -d \"$p\" ]; then printf ' COUNT ';"
            ' find "$p" -maxdepth 1 -type f 2>/dev/null | wc -l;'
            " else echo; fi;"
            "else echo MISSING; fi"
        )
        started = time.monotonic()
        try:
            result = await executor.run(command, timeout_s=15.0)
        except Exception as exc:  # transport failure -> unavailable
            return self._record(
                placement.placement_id,
                PlacementInspection(
                    placement_id=placement.placement_id,
                    state=InspectionState.UNAVAILABLE,
                    detail=str(exc)[:200],
                    checked_at=_checked_at(),
                ),
            )
        _ = started
        stdout = result.stdout.strip()
        state = InspectionState.MISSING
        if stdout.startswith("MISSING"):
            return self._record(
                placement.placement_id,
                PlacementInspection(
                    placement_id=placement.placement_id,
                    state=state,
                    checked_at=_checked_at(),
                ),
            )
        if not stdout.startswith("EXISTS"):
            return self._record(
                placement.placement_id,
                PlacementInspection(
                    placement_id=placement.placement_id,
                    state=InspectionState.UNAVAILABLE,
                    detail="unexpected inspection output",
                    checked_at=_checked_at(),
                ),
            )
        file_type = _parse_type(stdout)
        size_b = _parse_size(stdout)
        file_count = _parse_count(stdout)
        return self._record(
            placement.placement_id,
            PlacementInspection(
                placement_id=placement.placement_id,
                state=InspectionState.VERIFIED,
                file_type=file_type,
                size_b=size_b,
                file_count=file_count,
                checked_at=_checked_at(),
            ),
        )

    def _record(self, _key: str, inspection: PlacementInspection) -> PlacementInspection:
        self._latest[inspection.placement_id] = inspection
        return inspection


def _safe_path(remote_path: str) -> str:
    """Reject shell-hostile paths; the command still quotes everything."""
    candidate = remote_path.strip()
    if not candidate:
        raise ConflictError("remote path is empty")
    for char in ("\n", "\r", "\x00"):
        if char in candidate:
            raise ConflictError("remote path contains control characters")
    return candidate


def _parse_type(stdout: str) -> str | None:
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            return parts[1].split()[0] if parts[1].split() else None
    return None


def _parse_size(stdout: str) -> int | None:
    import re

    match = re.search(r"\s(\d+)$", stdout.splitlines()[0] if stdout else "")
    return int(match.group(1)) if match else None


def _parse_count(stdout: str) -> int | None:
    import re

    matches = re.findall(r"^(\d+)$", stdout, flags=re.MULTILINE)
    return int(matches[-1]) if matches else None


def _checked_at() -> str:
    from app.workspace.repository import utc_now

    return utc_now()

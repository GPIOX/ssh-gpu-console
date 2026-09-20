"""parse_progress2 byte-counter authority + completed-job progress snap.

rsync's --info=progress2 percentage is computed against a MOVING total
estimate (incremental recursion over large trees): the estimate starts small
and grows, so percent * our fixed du-based bytes_total sawtooths (field-observed:
2.5 GB -> 3.2 GB -> back to 2.6 GB) and the last progress line freezes at ~98%
while the trailing check phase transfers no bytes. These tests pin the raw
leading byte counter as the authoritative, monotonic source, the percent-only
fallback, and the service-side snap of bytes_done to bytes_total on completion.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

import pytest
from app.models.transfer import TransferJob, TransferRequest, TransferState, TransferStrategy
from app.transfer.planner import parse_progress2
from app.transfer.strategies import local_relay

from tests.test_transfer_service_resume import (
    Harness,
    _request,
    _wait_terminal,
)

_TOTAL = 3_400_313_654


def _job(**overrides: Any) -> TransferJob:
    """Minimal TransferJob for parser tests, mirroring the other test files."""
    payload: dict[str, Any] = {
        "job_id": "job-progress",
        "artifact_id": "art-1",
        "source_server_id": "srv-a",
        "source_path": "~/d.bin",
        "target_server_id": "srv-b",
        "target_path": "~/d.bin",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return TransferJob(**payload)


def test_byte_counter_wins_over_percent() -> None:
    """The leading comma-grouped counter is authoritative, not the percent."""
    job = _job(bytes_total=_TOTAL)
    parse_progress2("  2,812,345,678  87%  103.85MB/s    0:00:25 (xfr#1, to-chk=1/9)", job)
    assert job.bytes_done == 2_812_345_678  # NOT 87% x total = 2,958,272,879


def test_byte_counter_is_monotonic() -> None:
    """A stale/regressed line must never move the bar backwards."""
    job = _job(bytes_total=_TOTAL)
    parse_progress2("  2,812,345,678  87%  103.85MB/s    0:00:25 (xfr#1, to-chk=1/9)", job)
    parse_progress2("  1,000,000,000  30%  99.00MB/s    0:00:40 (xfr#1, to-chk=5/9)", job)
    assert job.bytes_done == 2_812_345_678


def test_percent_only_line_falls_back_to_percent_times_total() -> None:
    """rsync variants without a leading counter keep today's percent behavior."""
    job = _job(bytes_total=10_000)
    parse_progress2("  45%  10.00MB/s 0:00:25", job)
    assert job.bytes_done == 4_500


def test_rate_and_eta_parse_from_byte_counter_line() -> None:
    job = _job(bytes_total=_TOTAL)
    parse_progress2("  2,812,345,678  87%  103.85MB/s    0:00:25 (xfr#1, to-chk=1/9)", job)
    assert job.rate_bps == pytest.approx(103_850_000)
    assert job.eta_s == 25


def test_over_total_count_left_raw_clamp_lives_at_call_sites() -> None:
    """parse_progress2 alone may exceed bytes_total; the strategy call sites
    clamp (service _on_stdout caps bytes_done at bytes_total)."""
    job = _job(bytes_total=1_000)
    parse_progress2("  1,234  87%  10.00MB/s    0:00:25 (xfr#1, to-chk=1/9)", job)
    assert job.bytes_done == 1_234


def test_summary_line_is_not_parsed_as_progress() -> None:
    """The final 'sent ...' summary carries no percent and must not touch
    the counters or rate (why the byte regex is line-start anchored)."""
    job = _job(bytes_total=_TOTAL)
    parse_progress2("sent 2,812,345,678 bytes  received 1,234 bytes  103.85MB/s", job)
    assert job.bytes_done == 0
    assert job.rate_bps is None
    assert job.eta_s is None


# ---- service-level: completed jobs snap to the full bar ------------------------------


@pytest.mark.asyncio
async def test_completed_job_snaps_bytes_done_to_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After COMPLETED, bytes_done == bytes_total even when the strategy left
    the counter short (rsync's trailing check phase stops short of 100%)."""
    harness = Harness(tmp_path)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/m.bin", b"m" * 1024)
    artifact = harness.artifact("dataset", "M")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/m.bin")

    async def relay_transfer(
        *,
        job: Any,
        source: Any,
        target: Any,
        chunk_size: int,
        immutable: bool = False,
        excludes: tuple[str, ...] | list[str] = (),
        progress_interval_s: float = 0.5,
    ) -> None:
        del immutable, excludes, progress_interval_s
        job.bytes_total = 1024
        job.bytes_done = int(1024 * 0.98)  # rsync's last progress line: ~98%
        reader = await source.open_reader(job.source_path)
        data = bytearray()
        while True:
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            data += chunk
        await reader.close()
        directory = posixpath.dirname(job.target_path) or "."
        await target.mkdir(directory)
        writer = await target.open_writer(job.target_path)
        await writer.write(bytes(data))
        await writer.close()

    monkeypatch.setattr(local_relay, "relay_transfer", relay_transfer)

    service = harness.new_service()
    request: TransferRequest = _request(
        harness, artifact.artifact_id, placement.placement_id, "~/out/m.bin"
    )
    created = service.create(request)
    job = await _wait_terminal(service, created["job_id"])
    assert job.state == TransferState.COMPLETED
    assert job.bytes_total == 1024
    assert job.bytes_done == job.bytes_total

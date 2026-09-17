"""Phase 3 transfer tests: registry target locks and the size-checking
quick-verify re-walk.

Self-contained fakes: an in-memory TransferSession stub implementing only
stat/listdir (all quick_verify uses); no transport modules are imported and
nothing here depends on the Phase 2 test file.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.core.errors import ConflictError
from app.models.transfer import TransferJob, TransferState, TransferStrategy
from app.ssh.file_transfer import FileStat
from app.transfer.state import JobRegistry
from app.transfer.verifier import quick_verify

# ---- fakes ---------------------------------------------------------------------------


class MemSession:
    """Minimal in-memory TransferSession over dirs + file contents."""

    def __init__(self) -> None:
        self.dirs: set[str] = set()
        self.files: dict[str, bytes] = {}

    def add_dir(self, path: str) -> None:
        self.dirs.add(path)

    def add_file(self, path: str, content: bytes) -> None:
        self.files[path] = content

    async def stat(self, path: str) -> FileStat:
        if path in self.files:
            return FileStat(exists=True, is_dir=False, size_b=len(self.files[path]))
        if path in self.dirs:
            return FileStat(exists=True, is_dir=True)
        return FileStat(exists=False, is_dir=False)

    async def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [
            entry[len(prefix) :]
            for entry in (*self.files, *self.dirs)
            if entry.startswith(prefix) and "/" not in entry[len(prefix) :]
        ]


# ---- helpers -------------------------------------------------------------------------


def _registry_create(registry: JobRegistry, **overrides: Any) -> TransferJob:
    payload: dict[str, Any] = {
        "artifact_id": "a1",
        "artifact_label": "A:v1",
        "source_server_id": "srv-a",
        "source_path": "~/data",
        "target_server_id": "srv-b",
        "target_path": "~/mirror",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return registry.create(**payload)


def _job(**overrides: Any) -> TransferJob:
    """Direct TransferJob for verifier tests; counters unset -> re-walk path."""
    payload: dict[str, Any] = {
        "job_id": "j1",
        "artifact_id": "a1",
        "source_server_id": "srv-a",
        "source_path": "~/data",
        "target_server_id": "srv-b",
        "target_path": "~/mirror",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return TransferJob(**payload)


# ---- target locks ----------------------------------------------------------------------


def test_target_lock_blocks_second_job_until_first_is_terminal() -> None:
    registry = JobRegistry(10)
    first = _registry_create(registry)
    with pytest.raises(ConflictError, match="already has an active transfer"):
        _registry_create(registry)
    registry.transition(first, TransferState.COMPLETED)
    second = _registry_create(registry)  # terminal release frees the target
    assert second.job_id != first.job_id


def test_target_locks_are_per_server_and_per_path() -> None:
    registry = JobRegistry(10)
    _registry_create(registry)
    _registry_create(registry, target_path="~/mirror2")  # different path: fine
    _registry_create(registry, target_server_id="srv-c")  # different server: fine
    assert registry.active_count() == 3


def test_target_lock_normalizes_paths() -> None:
    registry = JobRegistry(10)
    _registry_create(registry, target_path="/a/b/")
    with pytest.raises(ConflictError, match="/a/b"):
        _registry_create(registry, target_path="/a/b")
    other = JobRegistry(10)
    _registry_create(other, target_path="~/x")
    with pytest.raises(ConflictError, match="~/x"):
        _registry_create(other, target_path="~/x/")


@pytest.mark.asyncio
async def test_target_lock_released_on_cancel() -> None:
    registry = JobRegistry(10)
    job = _registry_create(registry, target_path="~/staging")
    assert job.state == TransferState.QUEUED
    registry.transition(job, TransferState.CANCELLED)
    assert registry.target_lock_holder("srv-b", "~/staging") is None
    _registry_create(registry, target_path="~/staging")  # no conflict after cancel


@pytest.mark.asyncio
async def test_target_lock_released_on_failure() -> None:
    registry = JobRegistry(10)
    job = _registry_create(registry)
    registry.fail(job, "transfer_error", "session lost")
    assert registry.target_lock_holder("srv-b", "~/mirror") is None
    _registry_create(registry)


def test_target_lock_holder_reports_holder_or_none() -> None:
    registry = JobRegistry(10)
    assert registry.target_lock_holder("srv-b", "~/mirror") is None
    job = _registry_create(registry)
    # holder lookup goes through the same normalization as the lock itself
    assert registry.target_lock_holder("srv-b", "~/mirror/") == job.job_id
    assert registry.target_lock_holder("srv-b", "~/other") is None
    assert registry.target_lock_holder("srv-c", "~/mirror") is None


# ---- quick verify re-walk size parity ----------------------------------------------------


@pytest.mark.asyncio
async def test_verify_rewalk_fails_when_target_size_differs() -> None:
    """Phase 3 test list #15: a mirrored file that exists but carries a
    different size than its source must fail verification, naming the path."""
    source, target = MemSession(), MemSession()
    source.add_dir("~/data")
    source.add_file("~/data/weights.bin", b"w" * 4096)
    target.add_dir("~/mirror")
    target.add_file("~/mirror/weights.bin", b"w" * 1024)  # truncated copy

    job = _job()  # files_total/bytes_total unset -> re-walk path
    ok, reason = await quick_verify(job=job, source=source, target=target)
    assert not ok
    assert "~/mirror/weights.bin" in reason


@pytest.mark.asyncio
async def test_verify_rewalk_passes_when_sizes_match() -> None:
    source, target = MemSession(), MemSession()
    source.add_dir("~/data")
    source.add_file("~/data/weights.bin", b"w" * 4096)
    source.add_dir("~/data/sub")
    source.add_file("~/data/sub/piece.bin", b"p" * 256)
    target.add_dir("~/mirror")
    target.add_dir("~/mirror/sub")
    target.add_file("~/mirror/weights.bin", b"w" * 4096)
    target.add_file("~/mirror/sub/piece.bin", b"p" * 256)
    target.add_file("~/mirror/EXTRA.txt", b"stale")  # extra target files stay fine

    job = _job()
    ok, reason = await quick_verify(job=job, source=source, target=target)
    assert ok, reason

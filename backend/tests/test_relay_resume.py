"""Phase 3 relay tests: resume, incremental sync, symlink safety, progress.

Self-contained fakes (zero network): an in-memory tree with mtimes and
symlink entries plus a TransferSession fake whose offset reader/writer have
real resume semantics (append at an offset, never truncating; closing
flushes, like SFTP), so meta-checked resume and skip decisions can be
asserted byte-exactly.
"""

from __future__ import annotations

import itertools
import json
import posixpath
from typing import Any

import pytest
from app.models.transfer import TransferJob, TransferState, TransferStrategy
from app.ssh.file_transfer import FileStat
from app.transfer.state import JobRegistry
from app.transfer.strategies import local_relay
from app.transfer.strategies.local_relay import (
    CancelRequested,
    meta_name,
    partial_name,
    relay_transfer,
)

# ---- in-memory TransferSession fakes ------------------------------------------------


class MemFS:
    """In-memory tree: dirs, files with mtimes, and symlink entries."""

    def __init__(self) -> None:
        self.dirs: set[str] = {"/"}
        self.files: dict[str, bytes] = {}
        self.symlinks: dict[str, str] = {}  # link path -> pointed-to path
        self.mtimes: dict[str, int] = {}

    def _entry_stat(self, path: str, *, follow: bool) -> FileStat:
        if path in self.symlinks:
            if not follow:
                return FileStat(exists=True, is_dir=False, is_symlink=True)
            return self._entry_stat(self.symlinks[path], follow=True)
        if path in self.files:
            return FileStat(
                exists=True,
                is_dir=False,
                size_b=len(self.files[path]),
                mtime_s=self.mtimes.get(path, 0),
            )
        if path in self.dirs or path == "/" or path == "~":
            return FileStat(exists=True, is_dir=True)
        return FileStat(exists=False, is_dir=False)

    def stat(self, path: str) -> FileStat:
        return self._entry_stat(path, follow=True)

    def lstat(self, path: str) -> FileStat:
        return self._entry_stat(path, follow=False)

    def add_file(self, path: str, content: bytes, mtime_s: int = 0) -> None:
        self.files[path] = content
        if mtime_s:
            self.mtimes[path] = mtime_s

    def add_dir(self, path: str) -> None:
        self.dirs.add(path)

    def add_symlink(self, path: str, link_target: str) -> None:
        self.symlinks[path] = link_target


class FakeTransferSession:
    """TransferSession over MemFS with real offset semantics + call logs."""

    def __init__(self, fs: MemFS) -> None:
        self._fs = fs
        self.opened_readers: list[tuple[str, int]] = []  # (path, offset)
        self.opened_writers: list[tuple[str, int, bool]] = []  # (path, offset, truncate)
        self.streamed_from_source = 0  # bytes actually returned by readers
        self.mtime_calls: list[tuple[str, int]] = []
        self.removed: list[str] = []
        self.closed = False

    async def stat(self, path: str) -> FileStat:
        return self._fs.stat(path)

    async def lstat(self, path: str) -> FileStat:
        return self._fs.lstat(path)

    async def mkdir(self, path: str) -> None:
        self._fs.add_dir(path)

    async def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        entries = (*self._fs.files, *self._fs.dirs, *self._fs.symlinks)
        return [
            entry[len(prefix) :]
            for entry in entries
            if entry.startswith(prefix) and "/" not in entry[len(prefix) :]
        ]

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        if path not in self._fs.files:
            raise FileNotFoundError(path)
        self.opened_readers.append((path, offset))
        return _Reader(self._fs.files[path], offset, self)

    async def open_writer(self, path: str, *, offset: int = 0, truncate: bool = False) -> Any:
        self.opened_writers.append((path, offset, truncate))
        if offset > 0:
            if path not in self._fs.files:
                raise FileNotFoundError(path)  # resume never invents a file
            return _Writer(path, self._fs.files[path][:offset], self._fs)
        return _Writer(path, b"", self._fs)  # legacy 'wb' overwrite

    async def set_mtime(self, path: str, mtime_s: int) -> None:
        self.mtime_calls.append((path, mtime_s))
        self._fs.mtimes[path] = mtime_s

    async def rename(self, source_path: str, target_path: str) -> None:
        if source_path in self._fs.files:
            self._fs.files[target_path] = self._fs.files.pop(source_path)
            if source_path in self._fs.mtimes:
                self._fs.mtimes[target_path] = self._fs.mtimes.pop(source_path)

    async def remove(self, path: str) -> None:
        self.removed.append(path)
        self._fs.files.pop(path, None)
        self._fs.mtimes.pop(path, None)

    async def close(self) -> None:
        self.closed = True


class _Reader:
    def __init__(self, content: bytes, offset: int, session: FakeTransferSession) -> None:
        self._content = content
        self._pos = offset
        self._session = session

    async def read(self, size: int) -> bytes:
        chunk = self._content[self._pos : self._pos + size]
        self._pos += len(chunk)
        self._session.streamed_from_source += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


class _Writer:
    def __init__(self, path: str, base: bytes, fs: MemFS) -> None:
        self._path = path
        self._fs = fs
        self._buffer = bytearray(base)

    async def write(self, data: bytes) -> None:
        self._buffer += data

    async def close(self) -> None:
        self._fs.files[self._path] = bytes(self._buffer)  # closing flushes, like SFTP


class CancellingSource(FakeTransferSession):
    """Source that flips the job to cancelled once `limit` bytes have been
    streamed; the relay's next pre-chunk checkpoint raises CancelRequested."""

    def __init__(self, fs: MemFS, job: TransferJob, limit: int) -> None:
        super().__init__(fs)
        self._job = job
        self._limit = limit

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        inner = await super().open_reader(path, offset=offset)
        return _CancelAfterReader(inner, self._job, self, self._limit)


class _CancelAfterReader:
    def __init__(
        self, inner: Any, job: TransferJob, session: FakeTransferSession, limit: int
    ) -> None:
        self._inner = inner
        self._job = job
        self._session = session
        self._limit = limit

    async def read(self, size: int) -> bytes:
        chunk = await self._inner.read(size)
        if self._session.streamed_from_source >= self._limit:
            self._job.state = TransferState.CANCELLED
        return chunk

    async def close(self) -> None:
        await self._inner.close()


# ---- helpers ------------------------------------------------------------------------


def _job(**overrides: Any) -> TransferJob:
    payload: dict[str, Any] = {
        "job_id": "job-1",
        "artifact_id": "art-1",
        "artifact_label": "IVMSD:v1",
        "source_server_id": "srv-a",
        "source_path": "~/d.bin",
        "target_server_id": "srv-b",
        "target_path": "~/d.bin",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return TransferJob(**payload)


def _registry_job(registry: JobRegistry, **overrides: Any) -> TransferJob:
    payload: dict[str, Any] = {
        "artifact_id": "art-1",
        "artifact_label": "IVMSD:v1",
        "source_server_id": "srv-a",
        "source_path": "~/d.bin",
        "target_server_id": "srv-b",
        "target_path": "~/d.bin",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return registry.create(**payload)


def _side_paths(target_path: str) -> tuple[str, str]:
    directory = posixpath.dirname(target_path) or "."
    filename = posixpath.basename(target_path)
    return f"{directory}/{partial_name(filename)}", f"{directory}/{meta_name(filename)}"


def _meta_payload(
    job: TransferJob,
    *,
    source_path: str,
    target_path: str,
    source_size_b: int,
    source_mtime_s: int,
) -> bytes:
    return json.dumps(
        {
            "artifact_id": job.artifact_id,
            "source_path": source_path,
            "target_path": target_path,
            "source_size_b": source_size_b,
            "source_mtime_s": source_mtime_s,
        }
    ).encode("utf-8")


# ---- 1. single-file resume -----------------------------------------------------------


async def test_single_file_resumes_from_matching_meta() -> None:
    src, dst = MemFS(), MemFS()
    payload = b"0123456789"
    src.add_file("~/data/d.bin", payload, mtime_s=111)
    job = _job(source_path="~/data/d.bin", target_path="~/models/d.bin")
    partial, meta = _side_paths("~/models/d.bin")
    dst.add_file(partial, payload[:4])
    dst.add_file(
        meta,
        _meta_payload(
            job,
            source_path="~/data/d.bin",
            target_path="~/models/d.bin",
            source_size_b=10,
            source_mtime_s=111,
        ),
    )
    source, target = FakeTransferSession(src), FakeTransferSession(dst)

    await relay_transfer(job=job, source=source, target=target, chunk_size=4, immutable=True)

    assert source.opened_readers == [("~/data/d.bin", 4)]  # source read resumes at 4
    assert target.opened_writers == [(partial, 4, False)]  # partial appended, not truncated
    assert job.resumed_bytes == 4
    assert job.bytes_total == 10 and job.bytes_done == 10 and job.files_done == 1
    assert dst.files["~/models/d.bin"] == payload
    assert partial not in dst.files  # consumed by rename
    assert meta not in dst.files and meta in target.removed  # meta cleaned up
    assert target.mtime_calls == [("~/models/d.bin", 111)]  # mtime restored


# ---- 2. cancel mid-stream, retry resumes ----------------------------------------------


async def test_cancel_keeps_partial_and_meta_retry_streams_only_rest() -> None:
    src, dst = MemFS(), MemFS()
    payload = b"0123456789"
    src.add_file("~/d.bin", payload, mtime_s=7)
    registry = JobRegistry(10)
    job = _registry_job(registry)
    registry.transition(job, TransferState.RUNNING)
    partial, meta = _side_paths("~/d.bin")
    source = CancellingSource(src, job, limit=4)
    target = FakeTransferSession(dst)

    with pytest.raises(CancelRequested):
        await relay_transfer(job=job, source=source, target=target, chunk_size=4, immutable=False)

    assert source.streamed_from_source == 4
    assert dst.files[partial] == payload[:4]  # partial survives the cancel
    assert json.loads(dst.files[meta])["artifact_id"] == "art-1"  # meta survives too
    registry.transition(job, TransferState.CANCELLED)  # service bookkeeping afterwards

    retry = _registry_job(registry)  # NEW job_id, same target
    assert retry.job_id != job.job_id
    retry_source = FakeTransferSession(src)
    await relay_transfer(
        job=retry, source=retry_source, target=target, chunk_size=4, immutable=False
    )

    assert retry.resumed_bytes == 4
    assert retry_source.opened_readers == [("~/d.bin", 4)]
    assert retry_source.streamed_from_source == 6  # exactly total - already transferred
    assert dst.files["~/d.bin"] == payload
    assert partial not in dst.files and meta not in dst.files


# ---- 3. restart: resume does not depend on RAM job objects -----------------------------


async def test_resume_survives_full_registry_restart() -> None:
    src, dst = MemFS(), MemFS()
    payload = b"A" * 12
    src.add_file("~/d.bin", payload, mtime_s=9)
    first_registry = JobRegistry(10)
    job = _registry_job(first_registry)
    first_registry.transition(job, TransferState.RUNNING)
    source = CancellingSource(src, job, limit=5)

    with pytest.raises(CancelRequested):
        await relay_transfer(
            job=job,
            source=source,
            target=FakeTransferSession(dst),
            chunk_size=3,
            immutable=False,
        )
    first_registry.transition(job, TransferState.CANCELLED)
    partial, meta = _side_paths("~/d.bin")
    assert dst.files[partial] == payload[:6]  # 3+3 streamed before the checkpoint
    del first_registry, job, source  # nothing of the first attempt stays in RAM

    fresh_registry = JobRegistry(10)
    retry = _registry_job(fresh_registry)  # brand-new registry, brand-new job
    fresh_registry.transition(retry, TransferState.RUNNING)
    retry_source = FakeTransferSession(src)
    await relay_transfer(
        job=retry,
        source=retry_source,
        target=FakeTransferSession(dst),
        chunk_size=4,
        immutable=False,
    )

    assert retry.resumed_bytes == 6
    assert retry_source.streamed_from_source == 6  # 12 total - 6 already staged
    assert dst.files["~/d.bin"] == payload
    assert partial not in dst.files and meta not in dst.files


# ---- 4. changed source must not blind-resume -------------------------------------------


@pytest.mark.parametrize(
    ("new_payload", "new_mtime"),
    [
        (b"NEW CODE ENTIRELY", 200),  # size changed
        (b"0123456780", 300),  # same size, mtime moved
    ],
    ids=["size-changed", "mtime-changed"],
)
async def test_changed_source_retransfers_from_zero(new_payload: bytes, new_mtime: int) -> None:
    src, dst = MemFS(), MemFS()
    job = _job(source_path="~/code/app.py", target_path="~/srv/app.py")
    partial, meta = _side_paths("~/srv/app.py")
    dst.add_file(partial, b"0123")
    dst.add_file(
        meta,
        _meta_payload(
            job,
            source_path="~/code/app.py",
            target_path="~/srv/app.py",
            source_size_b=10,
            source_mtime_s=100,
        ),
    )
    src.add_file("~/code/app.py", new_payload, mtime_s=new_mtime)  # source moved on
    source, target = FakeTransferSession(src), FakeTransferSession(dst)

    await relay_transfer(job=job, source=source, target=target, chunk_size=4, immutable=False)

    assert job.resumed_bytes == 0
    assert source.opened_readers == [("~/code/app.py", 0)]  # streamed from zero
    # fresh meta (truncate) written BEFORE the partial is (re)opened
    assert target.opened_writers == [(meta, 0, True), (partial, 0, False)]
    assert dst.files["~/srv/app.py"] == new_payload  # stale partial never stitched in
    assert partial not in dst.files and meta not in dst.files


# ---- 5. up-to-date target file is skipped -----------------------------------------------


async def test_up_to_date_target_file_is_skipped_without_reading_source() -> None:
    src, dst = MemFS(), MemFS()
    payload = b"stable-bytes"
    src.add_file("~/dset/weights.bin", payload, mtime_s=500)
    dst.add_file("~/mirror/dset/weights.bin", payload, mtime_s=500)
    job = _job(source_path="~/dset/weights.bin", target_path="~/mirror/dset/weights.bin")
    source, target = FakeTransferSession(src), FakeTransferSession(dst)

    await relay_transfer(job=job, source=source, target=target, chunk_size=4, immutable=True)

    assert source.opened_readers == []  # no byte pulled from the source
    assert target.opened_writers == []
    assert job.files_total == 1 and job.files_done == 1 and job.files_skipped == 1
    assert job.bytes_total == 12 and job.bytes_done == 12 and job.bytes_skipped == 12
    assert dst.files["~/mirror/dset/weights.bin"] == payload


# ---- 6. size mismatch retransfers --------------------------------------------------------


async def test_size_mismatch_retransfers_file() -> None:
    src, dst = MemFS(), MemFS()
    payload = b"w" * 20
    src.add_file("~/dset/weights.bin", payload, mtime_s=500)
    dst.add_file("~/mirror/dset/weights.bin", b"w" * 12, mtime_s=500)
    job = _job(source_path="~/dset/weights.bin", target_path="~/mirror/dset/weights.bin")
    source, target = FakeTransferSession(src), FakeTransferSession(dst)

    await relay_transfer(job=job, source=source, target=target, chunk_size=8, immutable=True)

    assert job.files_skipped == 0 and job.bytes_skipped == 0
    assert source.opened_readers == [("~/dset/weights.bin", 0)]
    assert job.resumed_bytes == 0
    assert job.files_done == 1 and job.bytes_done == 20
    assert dst.files["~/mirror/dset/weights.bin"] == payload


# ---- 7. symlinks inside the tree are skipped with a warning ------------------------------


async def test_symlink_entries_are_skipped_with_warning() -> None:
    src, dst = MemFS(), MemFS()
    src.add_dir("~/dset")
    src.add_file("~/dset/weights.bin", b"w" * 8, mtime_s=5)
    src.add_symlink("~/dset/latest", "~/dset/weights.bin")
    job = _job(source_path="~/dset", target_path="~/mirror/dset")
    source, target = FakeTransferSession(src), FakeTransferSession(dst)

    await relay_transfer(job=job, source=source, target=target, chunk_size=8, immutable=True)

    assert "~/mirror/dset/latest" not in dst.files
    assert "~/mirror/dset/latest" not in dst.symlinks  # never recreated on the target
    assert dst.files["~/mirror/dset/weights.bin"] == b"w" * 8
    assert job.files_total == 1 and job.bytes_total == 8  # symlink never counted
    assert job.files_done == 1
    assert job.warnings == ["skipped symlink: ~/dset/latest"]


# ---- 8. deterministic partial naming ------------------------------------------------------


def test_partial_name_is_deterministic_across_jobs() -> None:
    assert partial_name("a.bin") == ".a.bin.sgc-partial"
    job_one = _job(job_id="job-one")
    job_two = _job(job_id="job-two")
    # no job_id parameter: different jobs derive the same scratch name and
    # therefore reuse each other's partials across retries/restarts
    assert partial_name(posixpath.basename(job_one.target_path)) == partial_name(
        posixpath.basename(job_two.target_path)
    )


# ---- 9. totals + rate/eta with a patched clock ---------------------------------------------


async def test_dir_totals_and_rate_eta_with_patched_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = MemFS(), MemFS()
    src.add_dir("~/dset")
    src.add_file("~/dset/staged.bin", b"s" * 8, mtime_s=42)  # target already has this one
    dst.add_dir("~/mirror/dset")
    dst.add_file("~/mirror/dset/staged.bin", b"s" * 8, mtime_s=42)
    src.add_file("~/dset/fresh.bin", b"f" * 12, mtime_s=42)
    job = _job(source_path="~/dset", target_path="~/mirror/dset")

    ticks = itertools.count()
    monkeypatch.setattr(local_relay, "_now", lambda: next(ticks) * 0.25)

    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=5,
        immutable=True,
        progress_interval_s=0,  # emit on every chunk after the baseline
    )

    assert job.files_total == 2 and job.bytes_total == 20
    assert job.files_done == 2 and job.files_skipped == 1 and job.bytes_skipped == 8
    assert job.bytes_done == 20  # skipped bytes count towards in-place progress
    assert job.rate_bps is not None and job.rate_bps > 0
    assert job.eta_s is not None and job.eta_s >= 0


# ---- 10. excludes + pre-cancel semantics do not regress -------------------------------------


async def test_excludes_still_filter_and_precancel_copies_nothing() -> None:
    src, dst = MemFS(), MemFS()
    src.add_dir("~/app")
    src.add_dir("~/app/dataset")
    src.add_dir("~/app/src")
    src.add_file("~/app/dataset/D1.bin", b"heavy" * 100)
    src.add_file("~/app/src/main.py", b"code")
    src.add_file("~/app/run.log", b"log")
    job = _job(
        job_id="job-excl",
        source_path="~/app",
        target_path="~/app-mirror",
        excludes=["dataset", "*.log"],
    )

    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=16,
        immutable=False,
        excludes=("dataset", "*.log"),
    )

    assert dst.files["~/app-mirror/src/main.py"] == b"code"
    assert "~/app-mirror/dataset/D1.bin" not in dst.files
    assert "~/app-mirror/run.log" not in dst.files
    assert job.files_total == 1 and job.files_done == 1

    cancelled = _job(job_id="job-cancel", source_path="~/app", target_path="~/out2")
    cancelled.state = TransferState.CANCELLED
    dst2 = MemFS()
    with pytest.raises(CancelRequested):
        await relay_transfer(
            job=cancelled,
            source=FakeTransferSession(src),
            target=FakeTransferSession(dst2),
            chunk_size=16,
            immutable=False,
        )
    assert not dst2.files  # nothing staged, nothing copied

"""LOCAL_RELAY: chunked SFTP copy A -> bounded local RAM -> B, resumable.

Guarantees (per WORKSPACE_SYNC_PLAN.md):
- NO local staging file; one bounded chunk (transfer_chunk_size_b) in RAM;
- copy/update semantics: never deletes anything on the target;
- every file is staged on the target as `<dir>/.<name>.sgc-partial` with a
  JSON meta sidecar `<dir>/.<name>.sgc-meta`; the partial is consumed by
  rename on success and the meta by remove — both are console-created
  scratch, never user data; failure/cancel keeps both for the retry;
- resume happens ONLY onto an exact meta match (same artifact, same source/
  target paths, source size AND source mtime unchanged) and only when the
  partial is a non-empty prefix of the source — a changed source, dataset
  or code, always retransfers from zero, never appends onto stale bytes;
- incremental sync: target files that already match the source (size +
  mtime) are skipped without reading the source;
- symlinks found inside a directory tree are never followed or copied;
- cancel between chunks leaves partials for a later retry;
- the walk only follows real directories/files reported by lstat.

Concurrency precondition: the registry's target lock guarantees at most one
active job per (target_server, target_path). This module relies on that —
partial/meta files are shared across retries and would be corrupted by two
concurrent writers.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import posixpath
import time
from collections import deque
from typing import Any

from app.models.transfer import TransferJob
from app.ssh.file_transfer import FileStat, TransferSession

# Test seam: rate/eta timing goes through this indirection so tests can
# patch a deterministic clock (monkeypatch local_relay._now).
_now = time.monotonic

_PARTIAL_SUFFIX = ".sgc-partial"
_META_SUFFIX = ".sgc-meta"
_META_KEYS = ("artifact_id", "source_path", "target_path", "source_size_b", "source_mtime_s")
_META_MAX_BYTES = 64 * 1024
_WARNINGS_MAX = 20


class CancelRequested(Exception):
    """Raised inside the copy loop when the user cancels the job."""


def partial_name(filename: str) -> str:
    """Deterministic scratch name (no job_id): a partial written by one job
    is reusable by whichever later job wins the same target lock."""
    return f".{filename}{_PARTIAL_SUFFIX}"


def meta_name(filename: str) -> str:
    return f".{filename}{_META_SUFFIX}"


def is_cancelled(job: TransferJob) -> bool:
    return job.state.value == "cancelled"


def is_excluded(name: str, excludes: tuple[str, ...] | list[str]) -> bool:
    """Entry-name match against the project's exclude patterns (fnmatch
    syntax, mirroring rsync's no-slash basename rule). The transfer root
    itself is never filtered — excludes only apply to walk children."""
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in excludes)


class _Progress:
    """Rate/eta tracker counting ONLY the bytes streamed from the source in
    this run (resumed take-over and incremental skips never inflate the
    rate), while bytes_done carries in-place bytes so bars resync to 100%.

    Rate is smoothed over a sliding window (~8s): trees of many tiny files
    move few bytes per 0.5s emit, and an unsmoothed rate would read ~0 B/s
    with an absurd ETA until a big file finally streams.
    """

    _WINDOW_S = 8.0

    def __init__(self, job: TransferJob, interval_s: float) -> None:
        self._job = job
        self._interval = interval_s
        self._streamed = 0
        self._samples: deque[tuple[float, int]] = deque()
        self._last_emit = 0.0

    def chunk(self, n: int) -> None:
        self._streamed += n
        now = _now()
        if self._last_emit == 0.0:
            self._last_emit = now  # first chunk: baseline only
        self._samples.append((now, self._streamed))
        while len(self._samples) > 1 and now - self._samples[0][0] > self._WINDOW_S:
            self._samples.popleft()
        if now - self._last_emit < self._interval:
            return
        oldest_t, oldest_s = self._samples[0]
        dt = now - oldest_t
        if dt > 0:
            rate = (self._streamed - oldest_s) / dt
            self._job.rate_bps = rate
            total = self._job.bytes_total
            if total is not None and rate > 0:
                self._job.eta_s = (total - self._job.bytes_done) / rate
        self._last_emit = now


async def relay_transfer(
    *,
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    chunk_size: int,
    immutable: bool,
    excludes: tuple[str, ...] | list[str] = (),
    progress_interval_s: float = 0.5,
) -> None:
    """Copy source_path (single file or recursive directory) A -> B.

    `immutable` marks dataset/model roots (True) vs code roots (False); BOTH
    take the identical resume validation — an append is only ever made onto
    a partial whose meta sidecar matches the CURRENT source size+mtime, so
    changed code is retransmitted from zero instead of being stitched onto
    stale bytes.

    Single-file roots use stat() (follows symlinks): an explicitly declared
    root symlink transfers the content it points at (the root is user
    declared and trusted). Symlinks found INSIDE a directory tree are never
    followed or copied — they are skipped with a bounded warning.

    Relies on the registry's target lock (one active job per target_server+
    target_path): partial/meta files are reused across retries and must not
    have concurrent writers.
    """
    source_stat = await source.stat(job.source_path)
    if not source_stat.exists:
        raise FileNotFoundError(f"source path does not exist: {job.source_path}")

    progress = _Progress(job, progress_interval_s)

    if not source_stat.is_dir:
        target_stat = await target.lstat(job.target_path)
        if _is_up_to_date(source_stat, target_stat):
            job.files_total = 1
            job.files_done = 1
            job.files_skipped = 1
            job.bytes_total = source_stat.size_b
            job.bytes_done = source_stat.size_b
            job.bytes_skipped = source_stat.size_b
            job.current_path = None
            return
        job.files_total = 1
        job.bytes_total = source_stat.size_b
        await _relay_file(
            job, source, target, job.source_path, job.target_path, chunk_size, progress=progress
        )
        job.files_done = 1
        job.current_path = None
        return

    await target.mkdir(job.target_path)
    files_total, bytes_total = await _count_tree(job, source, excludes)
    job.files_total = files_total
    job.bytes_total = bytes_total
    await _relay_dir(
        job,
        source,
        target,
        source_dir=job.source_path,
        target_dir=job.target_path,
        chunk_size=chunk_size,
        progress=progress,
        excludes=excludes,
    )
    job.current_path = None


async def _relay_file(
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    source_path: str,
    target_path: str,
    chunk_size: int,
    *,
    progress: _Progress,
) -> None:
    """Chunked copy of one file with meta-checked resume.

    The partial is named deterministically (no job_id) so the job that wins
    the target lock after a failure reuses the same scratch file, and the
    meta sidecar records exactly what the partial must match to be appended
    to: artifact_id, source_path, target_path, source_size_b, source_mtime_s.
    """
    job.current_path = source_path
    directory = posixpath.dirname(target_path) or "."
    filename = posixpath.basename(target_path)
    await target.mkdir(directory)
    partial = f"{directory}/{partial_name(filename)}"
    meta_path = f"{directory}/{meta_name(filename)}"

    source_stat = await source.stat(source_path)
    source_size = source_stat.size_b
    source_mtime = source_stat.mtime_s  # 0 == unknown

    resume_from = await _resume_offset(
        job,
        target,
        partial=partial,
        meta_path=meta_path,
        source_path=source_path,
        target_path=target_path,
        source_size=source_size,
        source_mtime=source_mtime,
    )
    if resume_from > 0:
        # Recovered bytes enter progress first: bars resync to 100%.
        job.resumed_bytes += resume_from
        job.bytes_done += resume_from
    else:
        await _write_meta(
            target,
            meta_path,
            job=job,
            source_path=source_path,
            target_path=target_path,
            source_size=source_size,
            source_mtime=source_mtime,
        )

    reader = await source.open_reader(source_path, offset=resume_from)
    writer = (
        await target.open_writer(partial, offset=resume_from)
        if resume_from > 0
        else await target.open_writer(partial)
    )
    try:
        while True:
            if is_cancelled(job):
                raise CancelRequested(job.job_id)
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            await writer.write(chunk)
            progress.chunk(len(chunk))
            job.bytes_done += len(chunk)
        await writer.close()
        await reader.close()
    except BaseException:
        await _close_quiet(reader)
        await _close_quiet(writer)
        raise

    await target.rename(partial, target_path)
    with contextlib.suppress(Exception):
        await target.remove(meta_path)  # console-created residue, never user data
    if source_mtime > 0:
        set_mtime = getattr(target, "set_mtime", None)
        if set_mtime is not None:
            with contextlib.suppress(Exception):
                await set_mtime(target_path, source_mtime)


async def _resume_offset(
    job: TransferJob,
    target: TransferSession,
    *,
    partial: str,
    meta_path: str,
    source_path: str,
    target_path: str,
    source_size: int,
    source_mtime: int,
) -> int:
    """Bytes of the existing partial that may be kept: only when its meta
    sidecar matches THIS artifact/source/target and the CURRENT source
    (size + mtime), and the partial is a non-empty prefix of the source."""
    partial_stat = await target.lstat(partial)
    if not partial_stat.exists or partial_stat.is_dir or partial_stat.size_b <= 0:
        return 0
    meta = await _read_meta(target, meta_path)
    if not _meta_matches(
        meta,
        job=job,
        source_path=source_path,
        target_path=target_path,
        source_size=source_size,
        source_mtime=source_mtime,
    ):
        return 0
    if partial_stat.size_b > source_size:
        return 0
    return partial_stat.size_b


async def _read_meta(target: TransferSession, meta_path: str) -> dict[str, Any] | None:
    """Parse the meta sidecar; anything missing or corrupt counts as absent."""
    reader: Any = None
    try:
        reader = await target.open_reader(meta_path)
        chunks: list[bytes] = []
        received = 0
        while received < _META_MAX_BYTES:
            chunk = await reader.read(4096)
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
        meta = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return None
    finally:
        if reader is not None:
            await _close_quiet(reader)
    return meta if isinstance(meta, dict) else None


def _meta_matches(
    meta: dict[str, Any] | None,
    *,
    job: TransferJob,
    source_path: str,
    target_path: str,
    source_size: int,
    source_mtime: int,
) -> bool:
    """Exactly the five recorded keys must match the CURRENT transfer."""
    if meta is None or set(meta) != set(_META_KEYS):
        return False
    return (
        meta["artifact_id"] == job.artifact_id
        and meta["source_path"] == source_path
        and meta["target_path"] == target_path
        and meta["source_size_b"] == source_size
        and meta["source_mtime_s"] == source_mtime
    )


async def _write_meta(
    target: TransferSession,
    meta_path: str,
    *,
    job: TransferJob,
    source_path: str,
    target_path: str,
    source_size: int,
    source_mtime: int,
) -> None:
    """Fresh-start marker, truncate-written BEFORE the first byte so a cancel
    mid-stream always leaves a meta that describes the partial next to it."""
    payload = json.dumps(
        {
            "artifact_id": job.artifact_id,
            "source_path": source_path,
            "target_path": target_path,
            "source_size_b": source_size,
            "source_mtime_s": source_mtime,
        }
    ).encode("utf-8")
    writer = await target.open_writer(meta_path, truncate=True)
    try:
        await writer.write(payload)
        await writer.close()
    except BaseException:
        await _close_quiet(writer)
        raise


def _is_up_to_date(source_stat: FileStat, target_stat: FileStat) -> bool:
    """Incremental-sync predicate: the target file already carries this
    source file (size + mtime agree; mtime 0 means unknown -> never skip)."""
    return (
        target_stat.exists
        and not target_stat.is_dir
        and not target_stat.is_symlink
        and target_stat.size_b == source_stat.size_b
        and source_stat.mtime_s > 0
        and target_stat.mtime_s == source_stat.mtime_s
    )


async def _close_quiet(handle: object) -> None:
    close = getattr(handle, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        result = close()
        if asyncio.iscoroutine(result):
            await result


async def _count_tree(
    job: TransferJob,
    source: TransferSession,
    excludes: tuple[str, ...] | list[str] = (),
) -> tuple[int, int]:
    """One summary walk over the source tree: file count and byte total.

    lstat-based: symlink entries are never counted (they are never copied).
    This is the first of at most two walks over a big tree (count + relay);
    the relay walk must not re-count or re-add bytes.
    """
    files = 0
    bytes_total = 0
    stack = [job.source_path]
    while stack:
        if is_cancelled(job):
            raise CancelRequested(job.job_id)
        current = stack.pop()
        for name in await source.listdir(current):
            # Defense in depth: sessions filter the SFTP dot entries; a
            # session that doesn't would loop forever on <dir>/. here.
            if name in (".", "..") or is_excluded(name, excludes):
                continue
            child = f"{current.rstrip('/')}/{name}"
            stat = await source.lstat(child)
            if not stat.exists:
                continue
            if stat.is_dir:
                stack.append(child)
            elif not stat.is_symlink:
                files += 1
                bytes_total += stat.size_b
    return files, bytes_total


async def _relay_dir(
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    *,
    source_dir: str,
    target_dir: str,
    chunk_size: int,
    progress: _Progress,
    excludes: tuple[str, ...] | list[str] = (),
) -> None:
    await target.mkdir(target_dir)
    names = await source.listdir(source_dir)
    for name in names:
        if name in (".", "..") or is_excluded(name, excludes):
            continue
        if is_cancelled(job):
            raise CancelRequested(job.job_id)
        source_child = f"{source_dir.rstrip('/')}/{name}"
        target_child = f"{target_dir.rstrip('/')}/{name}"
        child_stat = await source.lstat(source_child)
        if not child_stat.exists:
            continue  # vanished mid-walk
        if child_stat.is_symlink:
            if len(job.warnings) < _WARNINGS_MAX:
                job.warnings.append(f"skipped symlink: {source_child}")
            continue
        if child_stat.is_dir:
            await _relay_dir(
                job,
                source,
                target,
                source_dir=source_child,
                target_dir=target_child,
                chunk_size=chunk_size,
                progress=progress,
                excludes=excludes,
            )
        else:
            target_stat = await target.lstat(target_child)
            if _is_up_to_date(child_stat, target_stat):
                job.files_done += 1
                job.files_skipped += 1
                job.bytes_done += child_stat.size_b
                job.bytes_skipped += child_stat.size_b
                continue
            await _relay_file(
                job, source, target, source_child, target_child, chunk_size, progress=progress
            )
            job.files_done += 1

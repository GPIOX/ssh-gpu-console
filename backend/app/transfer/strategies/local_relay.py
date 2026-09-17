"""LOCAL_RELAY: chunked SFTP copy A -> bounded local RAM -> B.

Guarantees (per WORKSPACE_SYNC_PLAN.md):
- NO local staging file; one bounded chunk (transfer_chunk_size_b) in RAM;
- copy/update semantics: never deletes anything on the target;
- partial target files are written as `.<name>.sgc-partial-<job_id>` and
  renamed on completion (a good target is never replaced by a partial);
- cancel between chunks leaves partials for a later retry;
- the walk only follows real directories/files reported by stat.
"""

from __future__ import annotations

import asyncio
import posixpath

from app.models.transfer import TransferJob
from app.ssh.file_transfer import TransferSession


class CancelRequested(Exception):
    """Raised inside the copy loop when the user cancels the job."""


def partial_name(filename: str, job_id: str) -> str:
    return f".{filename}.sgc-partial-{job_id}"


def is_cancelled(job: TransferJob) -> bool:
    return job.state.value == "cancelled"


async def relay_transfer(
    *,
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    chunk_size: int,
    progress_every_bytes: int = 32 * 1024 * 1024,
) -> None:
    """Copy source_path (single file or recursive directory) A -> B."""
    source_stat = await source.stat(job.source_path)
    if not source_stat.exists:
        raise FileNotFoundError(f"source path does not exist: {job.source_path}")

    if not source_stat.is_dir:
        job.files_total = 1
        job.bytes_total = source_stat.size_b
        await _relay_file(job, source, target, job.source_path, job.target_path, chunk_size)
        job.files_done = 1
        job.current_path = None
        return

    await target.mkdir(job.target_path)
    job.files_total = await _count_tree(job, source)
    await _relay_dir(
        job,
        source,
        target,
        source_dir=job.source_path,
        target_dir=job.target_path,
        chunk_size=chunk_size,
        progress_every_bytes=progress_every_bytes,
    )
    job.current_path = None


async def _relay_file(
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    source_path: str,
    target_path: str,
    chunk_size: int,
) -> None:
    """Chunked SFTP copy of one file through the bounded RAM buffer."""
    job.current_path = source_path
    directory = posixpath.dirname(target_path) or "."
    filename = posixpath.basename(target_path)
    await target.mkdir(directory)
    partial = f"{directory}/{partial_name(filename, job.job_id)}"

    reader = await source.open_reader(source_path)
    writer = await target.open_writer(partial)
    try:
        while True:
            if is_cancelled(job):
                raise CancelRequested(job.job_id)
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            await writer.write(chunk)
            job.bytes_done += len(chunk)
        await writer.close()
        await reader.close()
    except BaseException:
        await _close_quiet(reader)
        await _close_quiet(writer)
        raise

    await target.rename(partial, target_path)


async def _close_quiet(handle: object) -> None:
    import contextlib

    close = getattr(handle, "close", None)
    if close is None:
        return
    with contextlib.suppress(Exception):
        result = close()
        if asyncio.iscoroutine(result):
            await result


async def _count_tree(job: TransferJob, source: TransferSession) -> int:
    total = 0
    stack = [job.source_path]
    while stack:
        if is_cancelled(job):
            raise CancelRequested(job.job_id)
        current = stack.pop()
        for name in await source.listdir(current):
            child = f"{current.rstrip('/')}/{name}"
            stat = await source.stat(child)
            if stat.exists and stat.is_dir:
                stack.append(child)
            elif stat.exists:
                total += 1
    return total


async def _relay_dir(
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
    *,
    source_dir: str,
    target_dir: str,
    chunk_size: int,
    progress_every_bytes: int = 32 * 1024 * 1024,
) -> None:
    await target.mkdir(target_dir)
    names = await source.listdir(source_dir)
    for name in names:
        if is_cancelled(job):
            raise CancelRequested(job.job_id)
        source_child = f"{source_dir.rstrip('/')}/{name}"
        target_child = f"{target_dir.rstrip('/')}/{name}"
        stat = await source.stat(source_child)
        if not stat.exists:
            continue  # vanished mid-walk
        if stat.is_dir:
            await _relay_dir(
                job,
                source,
                target,
                source_dir=source_child,
                target_dir=target_child,
                chunk_size=chunk_size,
            )
        else:
            await _relay_file(job, source, target, source_child, target_child, chunk_size)
            job.files_done += 1

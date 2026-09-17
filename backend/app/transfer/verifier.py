"""Quick verification of finished transfers (size / count parity only).

Strict hashing is intentionally NOT part of this phase.

Directories: the relay's counter walk and its copy walk are two separate
tree walks over a LIVE remote tree, so files can appear or vanish between
them (field-observed: files_done ended one above files_total). When the
job's own counters disagree, we therefore re-walk the source with the same
semantics as the relay and require only that every currently-existing
source file exists at the mirrored path on the target. Copy/update
semantics (the relay never deletes anything on the target) mean extra
files on the target — pre-existing files or .sgc-partial residue — are
always acceptable and never fail verification.
"""

from __future__ import annotations

import posixpath

from app.models.transfer import TransferJob
from app.ssh.file_transfer import TransferSession
from app.transfer.strategies.local_relay import is_excluded


async def quick_verify(
    *,
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
) -> tuple[bool, str]:
    """Verify a finished transfer by cheap parity checks.

    Single file: target size must equal the source size. Directories: when
    the job's own file counters agree, counter parity is enough (plus an
    optional cheap byte-total check); when they disagree, re-walk the
    source tree file-by-file. No remote SHA256 in this phase.
    """
    target_stat = await target.stat(job.target_path)
    if not target_stat.exists:
        return False, "target path does not exist"

    source_stat = await source.stat(job.source_path)
    if source_stat.exists and not source_stat.is_dir:
        if job.bytes_total is not None and target_stat.size_b != job.bytes_total:
            return (
                False,
                f"size mismatch: transferred {job.bytes_done} B, remote {target_stat.size_b} B",
            )
        if target_stat.size_b <= 0:
            return False, "target file is empty"
        return True, "size parity confirmed"

    # FAST PATH: the job's own counters agree AND were fully tracked (files
    # and bytes) — no tree changed under us, trust them. Relay directory
    # transfers leave bytes_total unset (None), so their counters alone are
    # not trustworthy evidence and the existence walk below runs instead.
    if (
        job.files_total is not None
        and job.files_done == job.files_total
        and job.bytes_total is not None
        and job.bytes_done == job.bytes_total
    ):
        return True, "counters parity confirmed"

    # SLOW PATH: counters disagree (files appeared/vanished mid-transfer on
    # the live tree) or bytes were never tracked. Re-walk the source with
    # the relay's exact walk semantics; every currently-existing source
    # file must exist on the target at the mirrored path (existence only —
    # sources may have changed since they were copied). Extra target files
    # are fine.
    missing = await _first_missing_target_file(job, source, target)
    if missing is not None:
        return False, f"file missing on target after re-walk: {missing}"
    return True, f"file-by-file re-verified: {job.files_done} files"


async def _first_missing_target_file(
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
) -> str | None:
    """Re-walk the source tree exactly like the relay's count/copy walks and
    return the FIRST source file whose mirror path is absent on the target
    (or None when every current source file is present)."""
    excludes = tuple(job.excludes)
    stack = [job.source_path]
    while stack:
        current = stack.pop()
        for name in await source.listdir(current):
            if name in (".", "..") or is_excluded(name, excludes):
                continue
            source_child = f"{current.rstrip('/')}/{name}"
            stat = await source.stat(source_child)
            if not stat.exists:
                continue  # vanished mid-walk, like the relay
            if stat.is_dir:
                stack.append(source_child)
                continue
            relative = posixpath.relpath(source_child, job.source_path)
            target_child = f"{job.target_path.rstrip('/')}/{relative}"
            target_child_stat = await target.stat(target_child)
            if not target_child_stat.exists:
                return target_child
    return None

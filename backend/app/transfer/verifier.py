"""Quick verification of finished transfers (size / count parity only).

Strict hashing is intentionally NOT part of this phase.
"""

from __future__ import annotations

from app.models.transfer import TransferJob
from app.ssh.file_transfer import TransferSession


async def quick_verify(
    *,
    job: TransferJob,
    source: TransferSession,
    target: TransferSession,
) -> tuple[bool, str]:
    """Verify a finished transfer by cheap parity checks.

    Single file: target size must equal the source size. Directories: the
    job's own byte/file counters must be consistent and the target directory
    must exist. No remote SHA256 in this phase.
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

    if job.files_total is not None and job.files_done != job.files_total:
        return (
            False,
            f"file count mismatch: {job.files_done}/{job.files_total} transferred",
        )
    if job.bytes_total is not None and job.bytes_done != job.bytes_total:
        return False, f"byte total mismatch: {job.bytes_done}/{job.bytes_total}"
    return True, "counters parity confirmed"

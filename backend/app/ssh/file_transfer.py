"""Transport-neutral file-transfer boundary (Sol-owned contract).

`app/transfer/*` depends only on these protocols; the asyncssh-backed
implementations live in `app/ssh/transport.py` (the only asyncssh importer).
A transfer never uses the telemetry ManagedConnection: it opens its own SSH
session with the same auth/host-key policy, so a telemetry timeout cannot
kill a running dataset transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class FileStat:
    """Capability boundary: SFTP v3 exposes mtime only in whole seconds, so
    ``mtime_s`` carries second precision (never nanoseconds)."""

    exists: bool
    is_dir: bool
    size_b: int = 0
    is_symlink: bool = False
    mtime_s: int = 0


class TransferSessionError(Exception):
    """Raised on transport failure while transferring files."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ServerLike(Protocol):
    """Minimal server identity for transfer-domain calls."""

    server_id: str
    ssh_host: str
    username: str | None
    port: int | None


@runtime_checkable
class TransferSession(Protocol):
    """SFTP-like session over one SSH connection to ONE server."""

    async def stat(self, path: str) -> FileStat: ...

    async def lstat(self, path: str) -> FileStat:
        """stat() without following symlinks (identifies a symlink entry)."""
        ...

    async def mkdir(self, path: str) -> None:
        """Create directory (including missing parents)."""
        ...

    async def listdir(self, path: str) -> list[str]:
        """Entry names of a directory (no types)."""
        ...

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        """Async file reader with awaitable read(n) and close().

        offset > 0 starts reading at that byte (resume reads).
        """
        ...

    async def open_writer(self, path: str, *, offset: int = 0, truncate: bool = False) -> Any:
        """Async file writer with write(data) and close().

        truncate=True overwrites from zero; offset>0 resumes AT that byte
        without truncating; the default keeps the legacy create/overwrite
        behavior for fresh partial files.
        """
        ...

    async def set_mtime(self, path: str, mtime_s: int) -> None:
        """Set mtime (seconds precision); failures propagate to the caller."""
        ...

    async def set_mode(self, path: str, mode: int) -> None:
        """Set the permission bits of a remote path (SFTP chmod); failures
        propagate to the caller (the direct-auth setup flow relies on 0700
        dirs / 0600 files being enforced, never silently skipped)."""
        ...

    async def rename(self, source: str, target: str) -> None: ...

    async def remove(self, path: str) -> None:
        """Remove a file (never recursive)."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class LongCommandRunner(Protocol):
    """Runs a long-lived remote command on ONE server, streaming output.

    Independent of the telemetry executor: dedicated connection, dedicated
    timeout, callback-driven (stdout lines / process end / cancellation).
    """

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        on_stdout: Any = None,  # async callable(str) — chunk callback
    ) -> int:
        """Run until completion; returns exit code (raises on transport loss)."""
        ...

    async def close(self) -> None: ...

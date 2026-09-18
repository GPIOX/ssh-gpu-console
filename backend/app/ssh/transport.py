"""AsyncSSH transport: the only module in the application that imports asyncssh.

Wiring:

- ``make_connect_factory(settings, trust)`` builds the connect factory used by
  ``SshManager``; it reads the user's OpenSSH environment (``~/.ssh/config``,
  SSH agent, IdentityFile passthrough, ``~/.ssh/known_hosts``) and never
  disables host-key checking, never copies key material, never stores
  passwords;
- the app-owned trust store (fingerprints only) is enforced by
  ``_HostKeyGate.validate_host_public_key``: keys the user's known_hosts
  already trust are accepted natively by AsyncSSH before this callback runs;
  fingerprints recorded in the trust store are accepted here; anything else
  is classified as unknown (captured for the explicit TOFU flow) or, when the
  host already has trusted entries, as a mismatch with no trust path;
- ``AsyncsshExecutor`` adapts one managed server to the transport-agnostic
  ``Executor`` protocol consumed by collectors.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncssh

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.server import HostKeyPrompt, ServerRecord
from app.ssh.config_resolver import ConnectParams
from app.ssh.connection import ConnectFactory, RemoteConnection
from app.ssh.errors import ConnectError, SshErrorCode, classify_connection_error
from app.ssh.executor import Executor, RemoteCommandResult
from app.ssh.known_hosts import HostKeyTrust

if TYPE_CHECKING:  # typing-only import keeps the manager -> transport edge one-way at runtime
    from app.ssh.manager import SshManager

logger = get_logger("ssh.transport")

USER_KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


@dataclass(slots=True)
class HostKeyDecision:
    """Captured result of the host-key gate for the current connect attempt."""

    captured: bool = False
    mismatch: bool = False
    fingerprint: str = ""
    key_type: str = ""


def fingerprint_from_key(key: asyncssh.SSHKey) -> str:
    """SHA256 fingerprint in OpenSSH display format ('SHA256:...')."""

    return key.get_fingerprint()


def key_type_of(key: asyncssh.SSHKey) -> str:
    line = key.export_public_key().decode("ascii")
    return line.split()[0]


class _HostKeyGate(asyncssh.SSHClient):
    """Distinguish unknown vs mismatched host keys; accept app-trusted ones."""

    def __init__(
        self,
        known_hosts: asyncssh.SSHKnownHosts,
        trust: HostKeyTrust,
        decision: HostKeyDecision,
        lookup_names: tuple[str, ...] = (),
    ) -> None:
        self._known_hosts = known_hosts
        self._trust = trust
        self._decision = decision
        # The host may be referenced by alias and by resolved address; a
        # known_hosts entry under either form counts as prior knowledge.
        self._lookup_names = lookup_names

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        fingerprint = fingerprint_from_key(key)
        if self._trust.is_trusted(host, port, fingerprint):
            return True
        entries, *_rest = self._known_hosts.match(host, addr, port)
        has_entries = bool(entries)
        for name in self._lookup_names:
            if name and name != host:
                name_entries, *_r = self._known_hosts.match(name, addr, port)
                if name_entries:
                    has_entries = True
                    break
        # A key the host already has entries for (user known_hosts or trust
        # store) that does not match them is a possible MITM, never a prompt.
        self._decision.captured = True
        self._decision.mismatch = has_entries or self._trust.has_host(host, port)
        self._decision.fingerprint = fingerprint
        self._decision.key_type = key_type_of(key)
        return False


def load_user_known_hosts(path: Path) -> asyncssh.SSHKnownHosts:
    """Parse the user's known_hosts; skip junk lines instead of failing all connects.

    An absent or unreadable file means nothing extra is trusted — verification
    is never disabled.
    """

    known_hosts = asyncssh.SSHKnownHosts()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return known_hosts
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            known_hosts.load(stripped)
        except (ValueError, asyncssh.KeyImportError):
            logger.warning("skipping malformed known_hosts line in %s", path)
    return known_hosts


def _host_key_error(
    params: ConnectParams, decision: HostKeyDecision, exc: Exception
) -> ConnectError:
    host = params.original_host or params.host
    if decision.mismatch:
        return ConnectError(
            SshErrorCode.HOST_KEY_MISMATCH,
            f"host key mismatch for {host}: presented {decision.fingerprint or 'unknown key'}"
            " — possible MITM; connection blocked",
            retryable=False,
        )
    prompt = HostKeyPrompt(
        host=host,
        port=params.port,
        key_type=decision.key_type or "unknown",
        fingerprint=decision.fingerprint,
    )
    return ConnectError(
        SshErrorCode.HOST_KEY_UNKNOWN,
        f"host key for {host} is not trusted yet",
        pending_host_key=prompt,
    )


async def _connect(
    params: ConnectParams,
    *,
    settings: Settings,
    trust: HostKeyTrust,
    user_known_hosts: Path,
) -> asyncssh.SSHClientConnection:
    known_hosts = load_user_known_hosts(user_known_hosts)
    decision = HostKeyDecision()
    lookup_names = tuple(dict.fromkeys(n for n in (params.original_host, params.host) if n))
    kwargs: dict[str, Any] = {
        # Connect to the RESOLVED address: the alias was already resolved by
        # our SSHConfigResolver and asyncssh's own config load is disabled.
        "host": params.host,
        "port": params.port,
        "known_hosts": known_hosts,
        "connect_timeout": settings.connect_timeout_s,
        "keepalive_interval": settings.keepalive_interval_s,
        "client_factory": lambda: _HostKeyGate(known_hosts, trust, decision, lookup_names),
        # NOTE: the user's ssh_config is intentionally NOT loaded by asyncssh
        # (config=[] disables its default ~/.ssh/config load): our resolver
        # already extracted host/user/port/IdentityFile/ProxyJump, and
        # asyncssh's stricter parser rejects trailing comments that real
        # OpenSSH tolerates (observed in the field). The SSH agent is still
        # used automatically via SSH_AUTH_SOCK.
        "config": [],
    }
    if params.username is not None:
        kwargs["username"] = params.username
    if params.identity_files:
        # Pass IdentityFile paths through; key material is never copied.
        kwargs["client_keys"] = [str(path) for path in params.identity_files]
    if params.proxy_jump:
        # Hops arrive PRE-RESOLVED ("[user@]host[:port]" via our resolver):
        # asyncssh resolves tunnel strings by DNS alone and must never see a
        # raw config alias (it would fail with getaddrinfo Errno 8).
        kwargs["tunnel"] = params.proxy_jump
    # NOTE: the user's ssh_config is intentionally NOT passed to asyncssh:
    # our resolver already extracted host/user/port/IdentityFile, and
    # asyncssh's stricter parser rejects some lines real OpenSSH tolerates
    # (observed: trailing comments). Agent forwarding still works via
    # SSH_AUTH_SOCK; agent-owned keys need no config entry.
    try:
        return await asyncssh.connect(**kwargs)
    except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
        raise
    except asyncssh.HostKeyNotVerifiable as exc:
        if not decision.captured:
            raise classify_connection_error(exc) from exc
        raise _host_key_error(params, decision, exc) from exc
    except Exception as exc:
        raise classify_connection_error(exc) from exc


def make_connect_factory(settings: Settings, trust: HostKeyTrust) -> ConnectFactory:
    """Build the default connect factory bound to the user's OpenSSH environment."""

    user_known_hosts = USER_KNOWN_HOSTS

    async def connect(params: ConnectParams) -> RemoteConnection:
        return await _connect(
            params, settings=settings, trust=trust, user_known_hosts=user_known_hosts
        )

    return connect


class AsyncsshExecutor:
    """``Executor`` bound to one registry server; delegates to the manager."""

    def __init__(self, manager: SshManager, server: ServerRecord) -> None:
        self._manager = manager
        self._server = server

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        return await self._manager.run(self._server, command, timeout_s=timeout_s)

    async def close(self) -> None:
        await self._manager.close_server(self._server.server_id)


def build_executor(manager: SshManager, server: ServerRecord) -> Executor:
    """Composition-root helper: adapt one managed server to the Executor protocol."""
    return AsyncsshExecutor(manager, server)


# ---- file-transfer runtime (the only asyncssh importer; transfer domain never sees it)

# asyncssh SFTP errors are NOT OSError subclasses (SFTPNoSuchFile.message is
# literally "No such file"); every sftp call must catch it beside the builtins.
from asyncssh.sftp import SFTPError, SFTPNoSuchFile  # noqa: E402

from app.ssh.file_transfer import FileStat  # noqa: E402

_MISSING_SFTP = (FileNotFoundError, SFTPNoSuchFile)


class SftpTransferSession:
    """TransferSession over one dedicated asyncssh SFTP client."""

    def __init__(
        self,
        connection: RemoteConnection,
        sftp: Any,
        settings: Settings,
    ) -> None:
        self._conn = connection
        self._sftp = sftp
        self.closed = False

    def is_usable(self) -> bool:
        """Health check without any network I/O.

        False once WE closed the session, or once the underlying SSH
        connection died (laptop sleep, network change, remote restart) —
        the state that previously surfaced only as asyncssh's
        'Connection not open' on the first SFTP operation.
        """
        return not self.closed and not self._conn.is_closed()

    async def stat(self, path: str) -> FileStat:
        import stat as stat_module

        try:
            info = await self._sftp.stat(path)
        except _MISSING_SFTP:
            return FileStat(exists=False, is_dir=False)
        mode = info.permissions or 0
        # stat() follows symlinks, so S_ISLNK is never set here. getattr keeps
        # minimal attrs objects usable (mtime is best-effort metadata).
        return FileStat(
            exists=True,
            is_dir=stat_module.S_ISDIR(mode),
            size_b=info.size or 0,
            is_symlink=stat_module.S_ISLNK(mode),
            mtime_s=int(getattr(info, "mtime", 0) or 0),
        )

    async def lstat(self, path: str) -> FileStat:
        import stat as stat_module

        try:
            info = await self._sftp.lstat(path)
        except _MISSING_SFTP:
            return FileStat(exists=False, is_dir=False)
        mode = info.permissions or 0
        return FileStat(
            exists=True,
            is_dir=stat_module.S_ISDIR(mode),
            size_b=info.size or 0,
            is_symlink=stat_module.S_ISLNK(mode),
            mtime_s=int(getattr(info, "mtime", 0) or 0),
        )

    async def mkdir(self, path: str) -> None:
        import posixpath

        current = ""
        parts = posixpath.normpath(path).strip("/").split("/")
        for part in parts:
            current = (
                f"{current}/{part}"
                if current.startswith("/") or current
                else (f"/{part}" if not current and path.startswith("/") else part)
            )
            try:
                await self._sftp.stat(current)
            except _MISSING_SFTP:
                try:
                    await self._sftp.mkdir(current)
                except (FileExistsError, SFTPError):
                    # Lost a creation race (or the server reports the level as
                    # existing): accept only when it is there now, else the
                    # next write fails with the real reason.
                    try:
                        await self._sftp.stat(current)
                    except _MISSING_SFTP:
                        raise

    async def listdir(self, path: str) -> list[str]:
        # SFTP readdir always includes "." and ".."; callers walk directories
        # recursively and would loop forever on <dir>/. — filter them here.
        entries = await self._sftp.readdir(path)
        return [entry.filename for entry in entries if entry.filename not in (".", "..")]

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        handle = await self._sftp.open(path, "rb")
        if offset > 0:
            await handle.seek(offset)
        return handle

    async def open_writer(self, path: str, *, offset: int = 0, truncate: bool = False) -> Any:
        # Legacy default stays 'wb' (create/overwrite): the relay writes fresh
        # partial files. Resume at a byte offset opens 'r+b' — never truncates;
        # a missing file fails loudly instead of silently restarting.
        if offset > 0:
            handle = await self._sftp.open(path, "r+b")
            await handle.seek(offset)
            return handle
        return await self._sftp.open(path, "wb")

    async def set_mtime(self, path: str, mtime_s: int) -> None:
        # Failures propagate: the caller decides whether mtime is best-effort.
        # SFTPv3 encodes ACMODTIME as an atime+mtime PAIR: passing mtime alone
        # drops the whole field group and setstat silently no-ops (field-
        # verified on OpenSSH sftp-server). One value goes to both fields.
        await self._sftp.setstat(path, asyncssh.SFTPAttrs(atime=mtime_s, mtime=mtime_s))

    async def rename(self, source: str, target: str) -> None:
        """Replace the destination atomically when possible.

        Plain SFTP rename FAILS when the target already exists (field-observed:
        every retry over an existing tree died with 'Failure' on the first
        file). posix-rename@openssh.com allows the overwrite; servers without
        the extension get an explicit remove+rename — updating the file being
        transferred is copy/UPDATE semantics, not a delete of untouched data.
        """
        try:
            await self._sftp.posix_rename(source, target)
        except (AttributeError, SFTPError):
            with contextlib.suppress(*_MISSING_SFTP):
                await self._sftp.remove(target)
            await self._sftp.rename(source, target)

    async def remove(self, path: str) -> None:
        await self._sftp.remove(path)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with contextlib.suppress(Exception):
            self._sftp.exit()
        with contextlib.suppress(Exception):
            self._conn.close()


class LongCommandSession:
    """LongCommandRunner over a dedicated asyncssh connection.

    Streams stdout through a callback so the rsync progress parser reads live
    lines; stderr is drained concurrently (optionally via ``on_stderr``) so
    its pipe can never fill and deadlock the remote process. Cancellation
    semantics: ``cancel()`` sets the flag AND closes the channel immediately;
    ``run()`` observes the flag at its 0.5s poll and raises
    ``asyncio.CancelledError`` within at most one poll interval. The closed
    channel makes the remote sshd terminate the rsync process, while rsync's
    ``--partial-dir`` preserves the local partial for a later resume.
    """

    def __init__(self, connection: RemoteConnection, settings: Settings) -> None:
        self._conn = connection
        self._cancelled = False
        self._process: Any | None = None

    def is_usable(self) -> bool:
        """Health check without any network I/O (same contract as
        SftpTransferSession.is_usable): False after cancel()/timeout or when
        the underlying SSH connection died."""
        return not self._cancelled and not self._conn.is_closed()

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        on_stdout: Any | None = None,
        on_stderr: Any | None = None,
    ) -> int:
        import asyncio

        async with self._conn.create_process(command) as process:
            self._process = process

            async def _reader(stream: Any, sink: Any | None) -> None:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return
                    if sink is not None:
                        await sink(chunk)

            reader = asyncio.create_task(_reader(process.stdout, on_stdout))
            # Stderr is ALWAYS drained (even with no consumer): a full pipe
            # would block the remote process and deadlock the command.
            stderr_reader = asyncio.create_task(_reader(process.stderr, on_stderr))
            deadline = asyncio.get_event_loop().time() + timeout_s
            try:
                while True:
                    if self._cancelled:
                        raise asyncio.CancelledError
                    _done, _pending = await asyncio.wait({reader}, timeout=0.5)
                    # Re-check AFTER the wait: cancel() must be observed within
                    # one poll interval even when the reader just hit EOF.
                    if self._cancelled:
                        raise asyncio.CancelledError
                    if reader.done():
                        break
                    if asyncio.get_event_loop().time() > deadline:
                        raise TimeoutError
                await asyncio.wait_for(reader, timeout=1.0)
                completed = await process.wait()
                # asyncssh's wait() returns an SSHCompletedProcess record, not
                # an int (field-verified on asyncssh 2.24): the previous
                # int(exit_code or 0) raised TypeError on every real run and
                # int-bearing stubs hid it from the tests.
                exit_code = (
                    completed
                    if isinstance(completed, int)
                    else getattr(completed, "exit_status", None)
                )
                # The channel closes only after the last stderr bytes were
                # buffered, but the drain task still needs one scheduling beat
                # to collect them — give it a bounded window so an on_stderr
                # collector never misses the tail.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stderr_reader, timeout=1.0)
                return int(exit_code or 0)
            except (TimeoutError, asyncio.CancelledError):
                self._cancelled = True
                raise
            finally:
                self._process = None
                for task in (reader, stderr_reader):
                    if not task.done():
                        task.cancel()

    async def cancel(self) -> None:
        # Closing the channel kills the remote process (sshd side) right away
        # instead of waiting for the next poll tick.
        self._cancelled = True
        process = self._process
        if process is not None:
            with contextlib.suppress(Exception):
                process.close()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()

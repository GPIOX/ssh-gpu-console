"""Bounded reusable SSH connection manager: one ManagedConnection per server.

Invariants:

- per-server entries are created under the manager lock and bounded
  (``_MAX_MANAGED``); a server's commands share one connection;
- a global semaphore (``max_in_flight_global``) and a per-server semaphore
  (``max_in_flight_per_server``) bound concurrent remote commands;
- ``close_server`` drains the per-server semaphore so running commands
  finish, while commands queued behind it fail with ``connection_lost``
  (the retire pattern);
- identity is ``(ssh_host casefolded, username, port)``: a PATCH that changes
  any part retires and rebuilds the connection on next use;
- runs against a connection in reconnect backoff fail fast, before queueing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.server import ConnectionTestResult, HostKeyPrompt, ServerRecord
from app.models.telemetry import ServerStatus
from app.ssh.config_resolver import ConnectParams, SSHConfigResolver, resolve_connect_params
from app.ssh.connection import ConnectFactory, ManagedConnection
from app.ssh.errors import ConnectError, SshErrorCode, SSHExecError
from app.ssh.executor import ExecutorError, RemoteCommandResult
from app.ssh.file_transfer import ServerLike
from app.ssh.known_hosts import HostKeyTrust, TrustedHostKey
from app.ssh.transport import LongCommandSession, SftpTransferSession, make_connect_factory

logger = get_logger("ssh.manager")

_MAX_MANAGED = 512

Identity = tuple[str, str | None, int | None]

# ConnectError codes -> ConnectionTestResult status values.
_STATUS_BY_CODE: dict[SshErrorCode, str] = {
    SshErrorCode.CONNECT_FAILED: ServerStatus.OFFLINE.value,
    SshErrorCode.CONNECTION_LOST: ServerStatus.OFFLINE.value,
    SshErrorCode.TIMEOUT: ServerStatus.TIMEOUT.value,
    SshErrorCode.AUTHENTICATION_FAILED: ServerStatus.AUTHENTICATION_FAILED.value,
    SshErrorCode.HOST_KEY_MISMATCH: ServerStatus.HOST_KEY_ERROR.value,
    SshErrorCode.HOST_KEY_UNKNOWN: ServerStatus.HOST_KEY_ERROR.value,
    SshErrorCode.RECONNECT_BACKOFF: ServerStatus.RECONNECTING.value,
    SshErrorCode.CANCELLED: ServerStatus.OFFLINE.value,
    SshErrorCode.UNKNOWN: ServerStatus.OFFLINE.value,
}


def _identity_of(server: ServerRecord) -> Identity:
    return (server.ssh_host.casefold(), server.username, server.port)


class SshManager:
    """Owns connection state, concurrency bounds and the host-key trust flow."""

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: SSHConfigResolver | None = None,
        trust_store: HostKeyTrust | None = None,
        connect_factory: ConnectFactory | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver or SSHConfigResolver()
        self.trust_store = trust_store or HostKeyTrust(settings.data_dir / "trusted_host_keys.json")
        self._connect_factory: ConnectFactory = connect_factory or make_connect_factory(
            settings, self.trust_store
        )
        self._managed: dict[str, ManagedConnection] = {}
        self._identities: dict[str, Identity] = {}
        self._server_slots: dict[str, asyncio.Semaphore] = {}
        # Transfers use dedicated sessions (never the telemetry connection):
        # same auth/host-key policy, independent lifecycle and slots.
        self._transfer_sessions: dict[str, SftpTransferSession | LongCommandSession] = {}
        self._transfer_slots: dict[str, asyncio.Semaphore] = {}
        # Serializes transfer-session creation per server (lazily created like
        # _transfer_slots): concurrent callers for one server share ONE session.
        self._transfer_session_locks: dict[str, asyncio.Lock] = {}
        self._retiring: set[str] = set()
        self._retire_events: dict[str, asyncio.Event] = {}
        self._global_slots = asyncio.Semaphore(settings.max_in_flight_global)
        self._lock = asyncio.Lock()

    # --- entry management ---

    async def _get_entry(self, server: ServerRecord) -> tuple[ManagedConnection, asyncio.Semaphore]:
        identity = _identity_of(server)
        async with self._lock:
            self._raise_if_retiring(server.server_id)
            connection = self._managed.get(server.server_id)
            stale = connection is not None and self._identities.get(server.server_id) != identity
        if stale:
            # Identity changed: retire (draining in-flight commands) and rebuild.
            await self.close_server(server.server_id)
        async with self._lock:
            self._raise_if_retiring(server.server_id)
            connection = self._managed.get(server.server_id)
            if connection is None:
                if len(self._managed) >= _MAX_MANAGED:
                    raise ConnectError(
                        SshErrorCode.UNKNOWN, "too many managed connections", retryable=False
                    )
                connection = ManagedConnection(
                    server.server_id, self.settings, self._connect_factory
                )
                self._managed[server.server_id] = connection
                self._identities[server.server_id] = identity
                self._server_slots[server.server_id] = asyncio.Semaphore(
                    self.settings.max_in_flight_per_server
                )
            return connection, self._server_slots[server.server_id]

    def _raise_if_retiring(self, server_id: str) -> None:
        if server_id in self._retiring:
            raise ConnectError(
                SshErrorCode.CONNECTION_LOST,
                "server connection is being closed",
                retryable=False,
            )

    def _params(self, server: ServerLike) -> ConnectParams:
        return resolve_connect_params(
            server.ssh_host,
            self.resolver,
            username=server.username,
            port=server.port,
        )

    # --- command path ---

    async def run(
        self, server: ServerRecord, command: str, *, timeout_s: float | None = None
    ) -> RemoteCommandResult:
        """Acquire/reuse the server's connection and run one fixed command."""

        effective_timeout = timeout_s if timeout_s is not None else self.settings.command_timeout_s
        try:
            await self._reject_if_backoff(server.server_id)
            async with self._global_slots:
                connection, slot = await self._get_entry(server)
                async with slot:
                    # close_server may have drained this slot while we were
                    # queued: never execute a command on a retired connection.
                    async with self._lock:
                        if (
                            server.server_id in self._retiring
                            or self._managed.get(server.server_id) is not connection
                        ):
                            raise ConnectError(
                                SshErrorCode.CONNECTION_LOST,
                                "server connection was closed",
                                retryable=False,
                            )
                    await self._acquire(server, connection)
                    return await connection.run(command, timeout_s=effective_timeout)
        except ConnectError as exc:
            raise ExecutorError(exc.code.value, exc.detail) from exc
        except SSHExecError as exc:
            raise ExecutorError(exc.code.value, exc.detail) from exc

    async def _reject_if_backoff(self, server_id: str) -> None:
        async with self._lock:
            connection = self._managed.get(server_id)
        if connection is not None and connection.in_backoff():
            raise ConnectError(
                SshErrorCode.RECONNECT_BACKOFF,
                f"reconnect backoff active; retry in {connection.backoff_remaining():.1f}s",
            )

    async def _acquire(self, server: ServerRecord, connection: ManagedConnection) -> None:
        if connection.is_open():
            return
        try:
            await connection.connect(self._params(server))
        except ConnectError:
            logger.info(
                "SSH connection failed for %s: %s", server.display_name, connection.last_error
            )
            raise

    # --- onboarding / trust ---

    async def test_connection(self, server: ServerRecord) -> ConnectionTestResult:
        """Explicit connection test for onboarding; bypasses backoff."""

        started = time.monotonic()
        connection, _slot = await self._get_entry(server)
        already_open = connection.is_open()
        try:
            await connection.connect(self._params(server), force=True)
        except ConnectError as exc:
            pending = (
                exc.pending_host_key if isinstance(exc.pending_host_key, HostKeyPrompt) else None
            )
            return ConnectionTestResult(
                ok=False,
                status=_STATUS_BY_CODE.get(exc.code, ServerStatus.OFFLINE.value),
                detail=exc.detail,
                latency_ms=(time.monotonic() - started) * 1000.0,
                pending_host_key=pending,
            )
        # A reused connection performs no handshake, so latency is meaningless.
        latency = None if already_open else (time.monotonic() - started) * 1000.0
        return ConnectionTestResult(
            ok=True,
            status=ServerStatus.ONLINE.value,
            detail="connected",
            latency_ms=latency,
        )

    async def trust_host_key(
        self, server: ServerRecord, prompt: HostKeyPrompt
    ) -> ConnectionTestResult:
        """Record explicit TOFU consent for the pending key, then re-test.

        The fingerprint must match the key currently presented by that host
        (captured during the last failed test). A mismatch is rejected and
        never persisted. Host-key mismatches never produce a pending key, so
        they have no trust path.
        """

        async with self._lock:
            connection = self._managed.get(server.server_id)
        if connection is None or connection.pending_host_key is None:
            return ConnectionTestResult(
                ok=False,
                status=ServerStatus.HOST_KEY_ERROR.value,
                detail="no pending host key; run a connection test first",
            )
        pending = connection.pending_host_key
        # Exact fingerprint compare: the base64 digest is case-sensitive.
        fingerprint_matches = pending.fingerprint.strip() == prompt.fingerprint.strip()
        if (
            not fingerprint_matches
            or pending.key_type != prompt.key_type
            or pending.host.casefold() != prompt.host.casefold()
        ):
            return ConnectionTestResult(
                ok=False,
                status=ServerStatus.HOST_KEY_ERROR.value,
                detail="fingerprint does not match the key presented by the host;"
                " re-run the connection test",
            )
        port = prompt.port if prompt.port is not None else pending.port
        self.trust_store.add(
            TrustedHostKey(
                host=prompt.host,
                port=port if port is not None else 22,
                key_type=prompt.key_type,
                fingerprint=prompt.fingerprint,
            )
        )
        connection.pending_host_key = None
        return await self.test_connection(server)

    # --- lifecycle ---

    async def note_connection_lost(self, server_id: str, detail: str) -> None:
        """Drop a known server's connection; unknown server_ids are ignored.

        Stale notifications must not create connections for servers that are
        no longer (or never were) managed.
        """

        async with self._lock:
            connection = self._managed.get(server_id)
        if connection is None:
            logger.debug("ignoring connection-lost note for unknown server %s", server_id)
            return
        connection.drop()
        logger.info("connection lost for %s: %s", server_id, detail)

    async def close_server(self, server_id: str) -> None:
        """Retire one server's connection, slots and identity immediately.

        The per-server semaphore is drained first so running commands finish;
        commands queued behind the drain fail their retiring check. Idempotent.
        """

        async with self._lock:
            existing_event = self._retire_events.get(server_id)
            if existing_event is not None:
                connection: ManagedConnection | None = None
                slot: asyncio.Semaphore | None = None
                retire_event = existing_event
                owner = False
            else:
                retire_event = asyncio.Event()
                self._retire_events[server_id] = retire_event
                self._retiring.add(server_id)
                connection = self._managed.get(server_id)
                slot = self._server_slots.get(server_id)
                owner = True

        if not owner:
            await retire_event.wait()
            return

        acquired = 0
        try:
            if connection is None or slot is None:
                async with self._lock:
                    self._pop_entry(server_id)
                return

            # Acquire every permit so no command remains active when the
            # ManagedConnection is closed. Queued commands re-check the
            # manager state in run() before executing.
            for _ in range(self.settings.max_in_flight_per_server):
                await slot.acquire()
                acquired += 1

            async with self._lock:
                if self._managed.get(server_id) is not connection:
                    return
                self._pop_entry(server_id)

            await connection.close()
            logger.info("closed SSH connection for server %s", server_id)
        finally:
            if slot is not None:
                for _ in range(acquired):
                    slot.release()
            async with self._lock:
                self._retiring.discard(server_id)
                event = self._retire_events.pop(server_id, None)
                if event is not None:
                    event.set()

    def _pop_entry(self, server_id: str) -> None:
        self._managed.pop(server_id, None)
        self._server_slots.pop(server_id, None)
        self._identities.pop(server_id, None)

    async def close_all(self) -> None:
        async with self._lock:
            server_ids = list(self._managed)
        await asyncio.gather(*(self.close_server(server_id) for server_id in server_ids))
        logger.info("closed %d SSH connection(s)", len(server_ids))

    async def connection_stats(self) -> list[dict[str, object]]:
        """Snapshot of managed connections; iteration happens under the lock."""

        async with self._lock:
            return [
                {
                    "server_id": server_id,
                    "state": connection.state,
                    "host": connection.params.host if connection.params else "",
                    "open": connection.is_open(),
                    "in_backoff": connection.in_backoff(),
                }
                for server_id, connection in self._managed.items()
            ]

    def resolve_params_for(self, server: ServerLike) -> ConnectParams:
        """ConnectParams for a server (public view used by the transfer domain)."""
        return self._params(server)

    # ---- transfer sessions ----------------------------------------------------

    async def transfer_session(
        self, server: ServerLike
    ) -> SftpTransferSession | LongCommandSession:
        """Dedicated SFTP session for one server (transfer-only, cached).

        Same auth/host-key rules as telemetry, but a separate connection so
        telemetry timeouts never kill an in-flight dataset transfer. A cached
        session is reused only when it is still usable: one whose underlying
        SSH connection died (laptop sleep, network change, remote restart) is
        closed and rebuilt here instead of being handed out dead (asyncssh's
        'Connection not open'). Creation is serialized per server, so
        concurrent callers for one server share exactly one session.
        """
        async with self._lock:
            session = self._transfer_sessions.get(server.server_id)
        if session is not None and not session.is_usable():
            await self._drop_dead_transfer_session(server.server_id, session)
            session = None
        if session is not None:
            return session
        lock = self._transfer_session_locks.get(server.server_id)
        if lock is None:
            # No await between get and set: the lazy creation is atomic.
            lock = asyncio.Lock()
            self._transfer_session_locks[server.server_id] = lock
        async with lock:
            # Double-check under the per-server lock: another caller for this
            # server may have created the session while we waited here.
            async with self._lock:
                cached = self._transfer_sessions.get(server.server_id)
            if cached is not None:
                if cached.is_usable():
                    return cached
                await self._drop_dead_transfer_session(server.server_id, cached)
            params = self._params(server)
            connection = await self._connect_factory(params)
            sftp = await connection.start_sftp_client()
            session = SftpTransferSession(connection, sftp, self.settings)
            async with self._lock:
                self._transfer_sessions[server.server_id] = session
            return session

    async def _drop_dead_transfer_session(
        self, server_id: str, session: SftpTransferSession | LongCommandSession
    ) -> None:
        """Pop an unusable session from the cache and close it.

        Every failure is suppressed: the session is already dead, and close
        must never block or fail the fresh creation that follows.
        """
        async with self._lock:
            if self._transfer_sessions.get(server_id) is session:
                self._transfer_sessions.pop(server_id, None)
        with contextlib.suppress(Exception):
            await session.close()

    async def transfer_command_session(self, server: ServerLike) -> LongCommandSession:
        """Dedicated connection for long-running transfer commands (rsync)."""
        params = self._params(server)
        connection = await self._connect_factory(params)
        session = LongCommandSession(connection, self.settings)
        return session

    def transfer_slot_or_create(self, server_id: str) -> asyncio.Semaphore:
        """Transfer-only per-server slot (never shares telemetry permits)."""
        slot = self._transfer_slots.get(server_id)
        if slot is None:
            slot = asyncio.Semaphore(self.settings.max_transfers_per_server)
            self._transfer_slots[server_id] = slot
        return slot

    async def close_transfer_sessions(self, server_id: str | None = None) -> None:
        """Close transfer sessions (all, or one server)."""
        if server_id is None:
            ids = list(self._transfer_sessions)
        else:
            ids = [server_id] if server_id in self._transfer_sessions else []
        for sid in ids:
            session = self._transfer_sessions.pop(sid, None)
            if session is not None:
                await session.close()

    async def close_transfer_command_session(self, server_id: str) -> None:
        """Close a server's dedicated long-command session (post-rsync)."""
        for known_id, session in list(self._transfer_sessions.items()):
            if known_id == server_id and isinstance(session, LongCommandSession):
                self._transfer_sessions.pop(known_id, None)
                await session.close()

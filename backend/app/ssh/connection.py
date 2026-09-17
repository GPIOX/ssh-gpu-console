"""One reusable managed connection with bounded reconnect state per server.

Lifecyle rules (GPUWatch lessons, ADR-0002):

- a command timeout drops the connection: after an in-run failure the channel
  state is uncertain, so the connection is never killed-and-reused;
- reconnect failures schedule a jittered exponential backoff (2-60 s);
  non-retryable failures (auth, host-key mismatch) do not;
- ``asyncio.CancelledError`` always propagates: cancellation is not a
  transport failure and must not poison the connection state.

This module never imports asyncssh: remote connections are accessed through
the structural ``RemoteConnection`` protocol so the transport dependency
stays inside ``transport.py``.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.server import HostKeyPrompt
from app.ssh.config_resolver import ConnectParams
from app.ssh.errors import ConnectError, SshErrorCode, SSHExecError, classify_connection_error
from app.ssh.executor import RemoteCommandResult

logger = get_logger("ssh.connection")

DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"
FAILED = "failed"

_CLOSE_WAIT_TIMEOUT_S = 5.0
_BACKOFF_JITTER = 0.25  # 0-25 % jitter keeps a fleet of offline hosts apart


@runtime_checkable
class RemoteConnection(Protocol):
    """Structural view of an open SSH client connection."""

    def start_sftp_client(self) -> Any:  # async: SFTP client (transfer domain)
        ...

    def create_process(self, command: str) -> Any:  # async context manager (rsync)
        ...

    def is_closed(self) -> bool: ...

    def close(self) -> None: ...

    def abort(self) -> None: ...

    async def wait_closed(self) -> None: ...

    async def run(self, command: str, *, check: bool = ..., timeout: float | None = ...) -> Any: ...


ConnectFactory = Callable[[ConnectParams], Awaitable[RemoteConnection]]


class ManagedConnection:
    """Connection lifecycle for exactly one registry server.

    The connect lock serializes connect/close transitions. Command
    parallelism is bounded by the manager's semaphores; the underlying
    connection safely carries multiple channels.
    """

    def __init__(self, server_id: str, settings: Settings, connect_factory: ConnectFactory) -> None:
        self.server_id = server_id
        self.settings = settings
        self._connect_factory = connect_factory
        self.state = DISCONNECTED
        self.conn: RemoteConnection | None = None
        self.params: ConnectParams | None = None
        self.attempts = 0
        self.next_attempt_at = 0.0
        self.last_error = ""
        self.pending_host_key: HostKeyPrompt | None = None
        self._connect_lock = asyncio.Lock()

    def is_open(self) -> bool:
        return self.conn is not None and not self.conn.is_closed()

    def backoff_delay(self) -> float:
        base = min(
            self.settings.reconnect_backoff_max_s,
            self.settings.reconnect_backoff_min_s * 2 ** max(0, self.attempts - 1),
        )
        return min(
            self.settings.reconnect_backoff_max_s, base * (1.0 + random.random() * _BACKOFF_JITTER)
        )

    def in_backoff(self) -> bool:
        return self.next_attempt_at > time.monotonic()

    def backoff_remaining(self) -> float:
        return max(0.0, self.next_attempt_at - time.monotonic())

    def _schedule_retry(self) -> None:
        self.attempts += 1
        self.next_attempt_at = time.monotonic() + self.backoff_delay()

    async def connect(self, params: ConnectParams, *, force: bool = False) -> None:
        async with self._connect_lock:
            if self.is_open():
                return
            if not force and self.in_backoff():
                raise ConnectError(
                    SshErrorCode.RECONNECT_BACKOFF,
                    f"reconnect backoff active; retry in {self.backoff_remaining():.1f}s",
                )
            self.state = CONNECTING
            self.pending_host_key = None
            try:
                connection = await self._connect_factory(params)
            except asyncio.CancelledError:
                self.state = DISCONNECTED
                raise
            except ConnectError as err:
                self._record_failure(err)
                if isinstance(err.pending_host_key, HostKeyPrompt):
                    self.pending_host_key = err.pending_host_key
                raise
            except BaseException as exc:  # classification boundary
                classified = classify_connection_error(exc)
                self._record_failure(classified)
                raise classified from exc
            self.conn = connection
            self.params = params
            self.state = CONNECTED
            self.attempts = 0
            self.next_attempt_at = 0.0
            self.last_error = ""

    def _record_failure(self, err: ConnectError) -> None:
        self.state = FAILED
        self.last_error = err.detail
        if err.retryable:
            self._schedule_retry()
        else:
            # Auth failures or host-key mismatches stay immediately re-testable.
            self.next_attempt_at = 0.0

    async def run(self, command: str, *, timeout_s: float) -> RemoteCommandResult:
        """Run one fixed command; drop the connection on timeout or transport loss."""

        connection = self.conn
        if connection is None or connection.is_closed():
            raise SSHExecError(SshErrorCode.CONNECTION_LOST, "SSH connection is not open")
        started = time.monotonic()
        try:
            # Double-watched budget: AsyncSSH enforces the per-process timeout,
            # wait_for is the hard backstop. Commands are fixed internal specs;
            # nothing user-provided reaches this string.
            process = await asyncio.wait_for(
                connection.run(command, check=False, timeout=timeout_s),
                timeout=timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            self.drop()
            raise SSHExecError(
                SshErrorCode.TIMEOUT,
                f"command timed out after {timeout_s:.1f}s; connection dropped",
            ) from exc
        except Exception as exc:
            # Any non-timeout failure mid-command means the channel/connection
            # state is no longer trustworthy: drop, never kill-and-reuse.
            self.drop()
            raise SSHExecError(
                SshErrorCode.CONNECTION_LOST,
                f"connection lost while running command: {exc}",
            ) from exc

        exit_status = getattr(process, "exit_status", None)
        return RemoteCommandResult(
            exit_code=exit_status if exit_status is not None else -1,
            stdout=str(getattr(process, "stdout", "")),
            stderr=str(getattr(process, "stderr", "")),
            duration_ms=(time.monotonic() - started) * 1000.0,
        )

    async def close(self) -> None:
        """Close and wait briefly; abort if the peer hangs (idempotent)."""

        async with self._connect_lock:
            connection, self.conn = self.conn, None
            self.state = DISCONNECTED
            self.pending_host_key = None
        if connection is None:
            return
        connection.close()
        try:
            await asyncio.wait_for(connection.wait_closed(), timeout=_CLOSE_WAIT_TIMEOUT_S)
        except (TimeoutError, OSError):
            connection.abort()

    def drop(self) -> None:
        """Abort the connection now and schedule a backoff reconnect."""

        connection, self.conn = self.conn, None
        if connection is not None:
            connection.abort()
        self.state = FAILED
        self.last_error = "connection dropped"
        self._schedule_retry()

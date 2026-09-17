"""SSH error taxonomy (Sol-owned contract).

Codes are the shared vocabulary across ssh layer, scheduler, state, API and UI.
`classify_connection_error` maps raw transport exceptions to typed codes by
message/type inspection so callers never import asyncssh to classify.
"""

from __future__ import annotations

import socket
from enum import StrEnum


class SshErrorCode(StrEnum):
    TIMEOUT = "timeout"
    CONNECT_FAILED = "connect_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    HOST_KEY_UNKNOWN = "host_key_unknown"
    CONNECTION_LOST = "connection_lost"
    RECONNECT_BACKOFF = "reconnect_backoff"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ConnectError(Exception):
    """Connection-level failure with a classified code.

    `pending_host_key` is set only for HOST_KEY_UNKNOWN: it carries the
    fingerprint awaiting explicit user trust (never auto-accepted).
    """

    def __init__(
        self,
        code: SshErrorCode,
        detail: str,
        *,
        retryable: bool = True,
        pending_host_key: object | None = None,
    ) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.pending_host_key = pending_host_key


class SSHExecError(Exception):
    """A command failed inside an established connection."""

    def __init__(self, code: SshErrorCode, detail: str, *, exit_code: int | None = None) -> None:
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.exit_code = exit_code


_AUTH_MARKERS = (
    "permission denied",
    "authentication",
    "auth fail",
    "private key",
    "publickey",
    "password",
)

_HOST_KEY_MISMATCH_MARKERS = (
    "key verification failed",  # asyncssh raises this when key mismatch
    "host key mismatch",
    "host key verification failed",
)


def _classify_message(message: str) -> SshErrorCode | None:
    lowered = message.lower()
    if "mismatch" in lowered or any(m in lowered for m in _HOST_KEY_MISMATCH_MARKERS):
        return SshErrorCode.HOST_KEY_MISMATCH
    if (
        "not in known_hosts" in lowered
        or "unknown host key" in lowered
        or "not received" in lowered
    ):
        return SshErrorCode.HOST_KEY_UNKNOWN
    if any(m in lowered for m in _AUTH_MARKERS):
        return SshErrorCode.AUTHENTICATION_FAILED
    return None


def classify_connection_error(exc: BaseException) -> ConnectError:
    """Map any connection-phase exception to a typed, retryable-flagged ConnectError."""
    if isinstance(exc, ConnectError):
        return exc
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        raise exc

    message = str(exc) or exc.__class__.__name__
    typed = _classify_message(message)
    if typed is not None:
        non_retryable = (SshErrorCode.HOST_KEY_MISMATCH, SshErrorCode.AUTHENTICATION_FAILED)
        return ConnectError(typed, message, retryable=typed not in non_retryable)
    if isinstance(exc, TimeoutError) or "timed out" in message.lower():
        return ConnectError(SshErrorCode.TIMEOUT, message)
    lowered = message.lower()
    dns_failure = "name or service not known" in lowered or "nodename nor servname" in lowered
    if isinstance(exc, socket.gaierror) or dns_failure:
        return ConnectError(SshErrorCode.CONNECT_FAILED, message)
    if isinstance(exc, ConnectionRefusedError):
        return ConnectError(SshErrorCode.CONNECT_FAILED, message)
    if isinstance(exc, (ConnectionError, BrokenPipeError, EOFError)):
        return ConnectError(SshErrorCode.CONNECTION_LOST, message)
    if isinstance(exc, OSError):
        return ConnectError(SshErrorCode.CONNECT_FAILED, message)
    return ConnectError(SshErrorCode.UNKNOWN, message, retryable=True)

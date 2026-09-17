"""Transport boundary: the only place the SSH implementation may leak.

Collectors, telemetry, and API code depend on the `Executor` protocol and
`RemoteCommandResult`. They must never import `asyncssh`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RemoteCommandResult:
    """Outcome of one remote command execution."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float


class ExecutorError(Exception):
    """Transport-level failure, already classified by the SSH layer."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@runtime_checkable
class Executor(Protocol):
    """Runs fixed, validated commands against one server.

    Implementations guarantee: bounded concurrency, command timeouts, and
    classification of failures into ExecutorError codes:
    timeout, connect_failed, authentication_failed, host_key_error, closed.
    """

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        """Execute one fixed command. Raises ExecutorError on transport failure."""
        ...

    async def close(self) -> None:
        """Release the underlying connection (idempotent)."""
        ...

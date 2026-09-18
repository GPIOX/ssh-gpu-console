"""Transfer-session health checks and probe-stderr surfacing (offline fakes).

Production failure being pinned: ``SshManager.transfer_session()`` used to
return a cached ``SftpTransferSession`` whenever its own ``closed`` flag was
False — but that flag only flips when WE close the session. After the
underlying SSH connection died (laptop sleep, network change, remote
restart), the first SFTP operation failed with asyncssh's
'Connection not open' one second into the transfer. The manager must now
health-check cached sessions (``is_usable()``) and rebuild dead ones, and
the batch-mode probe's ssh stderr must reach the plan reason.

Zero network: fake connections/sftp/factories, duck-typed sessions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.config import Settings
from app.models.transfer import TransferStrategy
from app.ssh.config_resolver import SSHConfigResolver
from app.ssh.executor import RemoteCommandResult
from app.ssh.known_hosts import HostKeyTrust
from app.ssh.manager import SshManager
from app.ssh.transport import LongCommandSession, SftpTransferSession
from app.transfer.planner import plan_transfer

# ---- fakes ---------------------------------------------------------------------------


class _FakeSftpClient:
    """SFTP client stand-in: only exit() (session close) is ever touched."""

    def __init__(self) -> None:
        self.exited = False

    def exit(self) -> None:
        self.exited = True


class _FakeConnection:
    """Duck RemoteConnection for the transfer-session cache path."""

    def __init__(self) -> None:
        self._closed = False
        self.close_calls = 0

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True
        self.close_calls += 1

    def abort(self) -> None:
        self._closed = True

    async def wait_closed(self) -> None:
        return None

    async def start_sftp_client(self) -> _FakeSftpClient:
        return _FakeSftpClient()


class _ProbeScriptSession:
    """Duck LongCommandSession: scripts the batch-mode probe outcome.

    The ssh stderr flows through the on_stderr collector exactly like the
    production failure ('Permission denied (publickey,password).' with
    exit 255); optionally raises instead to pin the exception path.
    """

    def __init__(
        self, stderr: str = "", exit_code: int = 255, error: Exception | None = None
    ) -> None:
        self._stderr = stderr
        self._exit = exit_code
        self._error = error
        self.commands: list[str] = []
        self.closed = False

    async def run(
        self,
        command: str,
        *,
        timeout_s: float,
        on_stdout: Any = None,
        on_stderr: Any = None,
    ) -> int:
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        if on_stderr is not None and self._stderr:
            await on_stderr(self._stderr)
        return self._exit

    async def close(self) -> None:
        self.closed = True


class _ProbeSsh:
    """Duck SshManager handing out the one scripted probe session."""

    def __init__(self, session: _ProbeScriptSession) -> None:
        self._session = session

    def resolve_params_for(self, server: Any) -> Any:
        return SimpleNamespace(host=server.ssh_host, port=server.port, username=server.username)

    async def transfer_command_session(self, server: Any) -> _ProbeScriptSession:
        return self._session


class _BrokenSsh:
    """Duck SshManager whose probe session creation fails (transport down)."""

    def resolve_params_for(self, server: Any) -> Any:
        return SimpleNamespace(host=server.ssh_host, port=server.port, username=server.username)

    async def transfer_command_session(self, server: Any) -> Any:
        raise ConnectionError("no route to host")


class _RsyncExecutor:
    """ExecutorLike double: rsync present on both ends, size probes empty."""

    async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
        if "command -v rsync" in command:
            return RemoteCommandResult(0, "/usr/bin/rsync\n", "", 1.0)
        return RemoteCommandResult(0, "", "", 1.0)


# ---- harness --------------------------------------------------------------------------


def _manager(tmp_path: Path, factory: Any) -> SshManager:
    settings = Settings(data_dir=tmp_path)
    return SshManager(
        settings,
        resolver=SSHConfigResolver(tmp_path / "ssh_config"),
        trust_store=HostKeyTrust(tmp_path / "trusted_host_keys.json"),
        connect_factory=factory,
    )


def _server(server_id: str) -> Any:
    return SimpleNamespace(
        server_id=server_id, ssh_host=f"{server_id}.example", username=None, port=22
    )


async def _plan(requested: TransferStrategy, ssh: Any) -> Any:
    return await plan_transfer(
        requested=requested,
        source_server=_server("src-host"),
        target_server=_server("dst-host"),
        source_executor=_RsyncExecutor(),
        target_executor=_RsyncExecutor(),
        source_path="/srv/data",
        target_path="/srv/data",
        artifact_id="artifact-1",
        ssh=ssh,
    )


# ---- 1. dead cached SFTP session is replaced, not returned -----------------------------


async def test_dead_cached_sftp_session_is_replaced(tmp_path: Path) -> None:
    connections: list[_FakeConnection] = []

    async def factory(_params: Any) -> _FakeConnection:
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    manager = _manager(tmp_path, factory)
    server = _server("srv-dead")
    first = await manager.transfer_session(server)
    assert isinstance(first, SftpTransferSession)

    # The underlying SSH connection died (laptop sleep / network change):
    # only the transport knows; the session's own closed flag stays False.
    connections[0]._closed = True
    assert first.closed is False
    assert first.is_usable() is False

    second = await manager.transfer_session(server)

    assert second is not first
    assert isinstance(second, SftpTransferSession)
    assert len(connections) == 2  # factory called again
    assert connections[0].close_calls == 1  # old session's close was attempted
    assert first.closed is True
    assert manager._transfer_sessions[server.server_id] is second  # new one is cached
    assert second.is_usable() is True
    await manager.close_transfer_sessions()


# ---- 2. live cached session is reused ---------------------------------------------------


async def test_live_cached_sftp_session_is_reused(tmp_path: Path) -> None:
    connections: list[_FakeConnection] = []

    async def factory(_params: Any) -> _FakeConnection:
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    manager = _manager(tmp_path, factory)
    server = _server("srv-live")
    first = await manager.transfer_session(server)
    second = await manager.transfer_session(server)

    assert second is first
    assert len(connections) == 1
    assert first.is_usable() is True
    await manager.close_transfer_sessions()
    assert connections[0].close_calls == 1


# ---- 3. concurrent callers share exactly one creation ------------------------------------


async def test_concurrent_callers_share_one_creation(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    connections: list[_FakeConnection] = []

    async def factory(_params: Any) -> _FakeConnection:
        entered.set()
        await release.wait()  # hold the per-server lock mid-connect
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    manager = _manager(tmp_path, factory)
    server = _server("srv-race")
    first_task = asyncio.create_task(manager.transfer_session(server))
    await entered.wait()  # first caller is inside connect(), holding the lock
    second_task = asyncio.create_task(manager.transfer_session(server))
    await asyncio.sleep(0.05)  # let the second caller reach the per-server lock
    release.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert first is second
    assert len(connections) == 1  # exactly ONE factory call
    await manager.close_transfer_sessions()


# ---- 4. is_usable() surface on both session types -----------------------------------------


async def test_is_usable_reflects_cancel_and_connection_state(tmp_path: Path) -> None:
    connection = _FakeConnection()
    long_session = LongCommandSession(connection, Settings())
    assert long_session.is_usable() is True
    await long_session.cancel()
    assert long_session.is_usable() is False  # cancelled -> not usable

    connection2 = _FakeConnection()
    sftp_session = SftpTransferSession(connection2, _FakeSftpClient(), Settings())
    assert sftp_session.is_usable() is True
    connection2._closed = True
    assert sftp_session.is_usable() is False  # connection died -> not usable
    await sftp_session.close()
    assert sftp_session.is_usable() is False  # we closed it -> still not usable


# ---- 5. probe stderr reaches the plan reason ------------------------------------------------


async def test_auto_plan_reason_carries_probe_stderr() -> None:
    session = _ProbeScriptSession("Permission denied (publickey,password).")
    plan = await _plan(TransferStrategy.AUTO, _ProbeSsh(session))

    assert plan.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert plan.reason.startswith("direct rsync unavailable: ")
    assert "Permission denied (publickey,password)." in plan.reason
    assert plan.reason.endswith("; using local relay")
    # security invariants of the probe command are untouched
    assert "BatchMode=yes" in session.commands[0]
    assert "StrictHostKeyChecking=yes" in session.commands[0]
    assert session.closed  # the probe session is closed after planning


async def test_direct_plan_reason_carries_probe_stderr() -> None:
    session = _ProbeScriptSession("Permission denied (publickey,password).")
    plan = await _plan(TransferStrategy.DIRECT_RSYNC, _ProbeSsh(session))

    assert plan.strategy_selected is None
    assert plan.reason.startswith("direct rsync unavailable: ")
    assert "Permission denied (publickey,password)." in plan.reason


async def test_probe_without_stderr_falls_back_to_exit_code() -> None:
    session = _ProbeScriptSession(stderr="", exit_code=255)
    plan = await _plan(TransferStrategy.AUTO, _ProbeSsh(session))

    assert plan.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert "ssh exited with code 255" in plan.reason


async def test_probe_exception_merges_into_detail() -> None:
    session = _ProbeScriptSession(error=ConnectionError("link down"))
    plan = await _plan(TransferStrategy.AUTO, _ProbeSsh(session))

    assert plan.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert "probe error: link down" in plan.reason


async def test_transport_failure_merges_into_detail() -> None:
    plan = await _plan(TransferStrategy.AUTO, _BrokenSsh())

    assert plan.strategy_selected is TransferStrategy.LOCAL_RELAY
    assert "preflight probe failed: no route to host" in plan.reason


async def test_multi_line_stderr_is_collapsed_to_one_line() -> None:
    session = _ProbeScriptSession("first line\nsecond line\n\nthird line")
    plan = await _plan(TransferStrategy.AUTO, _ProbeSsh(session))

    assert "first line; second line; third line" in plan.reason
    assert "\n" not in plan.reason


async def test_long_stderr_is_truncated_to_bounded_detail() -> None:
    session = _ProbeScriptSession("x" * 500)
    plan = await _plan(TransferStrategy.AUTO, _ProbeSsh(session))

    assert "x" * 160 in plan.reason
    assert "x" * 161 not in plan.reason

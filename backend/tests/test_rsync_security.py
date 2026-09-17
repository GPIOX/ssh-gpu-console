"""Phase 3 transport-security and cancellation tests (offline fakes, zero network).

Exemption basis (backend/AGENTS.md normally forbids importing the ssh transport
modules in tests): Sol explicitly approved importing `app.ssh.transport` in THIS
file for offline unit tests only — fake connections/processes, no real SSH — to
pin LongCommandSession's cancel semantics and SftpTransferSession's protocol
surface. rsync_transfer itself is exercised against a fake command session, so
no manager/network import is needed for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import shlex
from types import SimpleNamespace
from typing import Any, get_protocol_members

import pytest
from app.core.config import Settings
from app.models.transfer import TransferJob, TransferStrategy
from app.ssh.file_transfer import FileStat, TransferSession
from app.ssh.transport import LongCommandSession, SftpTransferSession
from app.transfer.planner import (
    _batch_mode_probe,
    build_rsync_command,
    target_rsync_spec,
)
from app.transfer.state import JobRegistry
from app.transfer.strategies.direct_rsync import CancelRequested, rsync_transfer

# ---- fakes ---------------------------------------------------------------------------


class _ProbeSession:
    """Duck LongCommandSession: records probe commands, never touches a network."""

    def __init__(self, exit_code: int = 0) -> None:
        self.commands: list[str] = []
        self._exit = exit_code

    async def run(self, command: str, *, timeout_s: float, on_stdout: Any = None) -> int:
        self.commands.append(command)
        return self._exit

    async def close(self) -> None:
        return None


class _FakeLongSession:
    """Duck LongCommandRunner with a scripted outcome; records close()."""

    def __init__(self, outcome: int | Exception) -> None:
        self.commands: list[str] = []
        self.closed = False
        self._outcome = outcome

    async def run(self, command: str, *, timeout_s: float, on_stdout: Any = None) -> int:
        self.commands.append(command)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def close(self) -> None:
        self.closed = True


class _FakeSshManager:
    """Duck SshManager: hands out the one scripted long-command session."""

    def __init__(self, session: _FakeLongSession) -> None:
        self._session = session

    def resolve_params_for(self, server: Any) -> Any:
        return SimpleNamespace(host=server.ssh_host, port=server.port, username=server.username)

    async def transfer_command_session(self, server: Any) -> _FakeLongSession:
        return self._session


class _FakeStdout:
    """Blocks in read() until the channel closes, then reports EOF."""

    def __init__(self, process: _FakeProcess) -> None:
        self._process = process

    async def read(self, size: int) -> bytes:
        await self._process.channel_closed.wait()
        return b""


class _FakeProcess:
    """Offline process stand-in: stdout.read blocks until the channel closes."""

    def __init__(self) -> None:
        self.closed = False
        self.channel_closed = asyncio.Event()
        self.stdout = _FakeStdout(self)

    async def __aenter__(self) -> _FakeProcess:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        self.closed = True
        self.channel_closed.set()

    async def wait(self) -> int:
        await self.channel_closed.wait()
        return 0


class _FakeConnection:
    """Duck RemoteConnection: create_process is sync, like asyncssh's."""

    def __init__(self) -> None:
        self.processes: list[_FakeProcess] = []
        self.closed = False

    def create_process(self, command: str) -> _FakeProcess:
        process = _FakeProcess()
        self.processes.append(process)
        return process

    def close(self) -> None:
        self.closed = True


def _server(ssh_host: str) -> Any:
    return SimpleNamespace(server_id=ssh_host, ssh_host=ssh_host, username="u", port=2222)


def _job() -> TransferJob:
    return JobRegistry(10).create(
        artifact_id="a1",
        artifact_label="A",
        source_server_id="srv-a",
        source_path="/s",
        target_server_id="srv-b",
        target_path="/d",
        strategy_requested=TransferStrategy.DIRECT_RSYNC,
    )


# ---- 1. strict host-key checking in probe AND in the rsync -e ssh options --------------


@pytest.mark.asyncio
async def test_probe_command_never_writes_known_hosts() -> None:
    session = _ProbeSession(exit_code=0)
    ok = await _batch_mode_probe(session, "target-host", 2222, "u")
    assert ok
    assert len(session.commands) == 1
    assert "StrictHostKeyChecking=yes" in session.commands[0]
    assert "accept-new" not in session.commands[0]


def test_rsync_ssh_option_is_strict_host_key_checking() -> None:
    for port in (None, 2222):
        command = build_rsync_command(source_path="/s", target_spec="u@h:/d", target_port=port)
        assert "StrictHostKeyChecking=yes" in command
        assert "accept-new" not in command


# ---- 2. port is encoded exactly once (inside -e), never inside the remote spec --------


def test_port_travels_only_via_ssh_option() -> None:
    spec = target_rsync_spec(host="h", username="u", target_path="/d")
    assert spec == "u@h:/d"
    command = build_rsync_command(source_path="/s", target_spec=spec, target_port=2222)
    assert command.count("2222") == 1
    assert "-p 2222" in command
    assert "h:2222:/d" not in command
    tokens = shlex.split(command)
    ssh_opts = tokens[tokens.index("-e") + 1]
    assert ssh_opts.startswith("ssh -o")
    assert ssh_opts.endswith("-p 2222")


def test_ipv6_target_spec_is_bracketed_without_port() -> None:
    spec = target_rsync_spec(host="::1", username=None, target_path="/d")
    # shlex-quoted once for the shell (brackets are shell-special), bracketed, no port
    assert shlex.split(spec) == ["[::1]:/d"]
    command = build_rsync_command(source_path="/s", target_spec=spec, target_port=None)
    assert "[::1]" in command


# ---- 3. rsync flags are locked to the safe fixed set -----------------------------------


def test_rsync_flags_locked_to_safe_set() -> None:
    command = build_rsync_command(
        source_path="/s",
        target_spec="u@h:/d",
        target_port=2222,
        excludes=("*.log", "cache"),
    )
    tokens = shlex.split(command)
    rsync_tokens = tokens[: tokens.index("-e")]
    for banned in ("--delete", "--append", "--append-verify", "-a", "-o", "-g"):
        assert banned not in rsync_tokens
    for required in (
        "-r",
        "-l",
        "-t",
        "-p",
        "--safe-links",
        "--partial",
        "--partial-dir=.sgc-rsync-partial",
        "--info=progress2",
    ):
        assert required in rsync_tokens
    # excludes travel after -e (they are rsync args, not part of the -e ssh options)
    assert "--exclude=*.log" in tokens and "--exclude=cache" in tokens
    assert tokens[tokens.index("--") + 1] == "/s"
    assert tokens[-1] == "u@h:/d"


# ---- 4. rsync_transfer closes its session on every exit path ---------------------------


@pytest.mark.asyncio
async def test_rsync_transfer_closes_session_on_success() -> None:
    session = _FakeLongSession(outcome=0)
    code = await rsync_transfer(
        job=_job(),
        ssh=_FakeSshManager(session),
        source_server=_server("src"),
        target_server=_server("dst"),
        source_size_b=None,
    )
    assert code == 0
    assert session.closed
    assert "-p 2222" in session.commands[0]
    assert "StrictHostKeyChecking=yes" in session.commands[0]


@pytest.mark.asyncio
async def test_rsync_transfer_closes_session_on_transport_error() -> None:
    session = _FakeLongSession(outcome=ConnectionError("link down"))
    with pytest.raises(ConnectionError):
        await rsync_transfer(
            job=_job(),
            ssh=_FakeSshManager(session),
            source_server=_server("src"),
            target_server=_server("dst"),
            source_size_b=None,
        )
    assert session.closed


@pytest.mark.asyncio
async def test_rsync_transfer_propagates_cancel_requested() -> None:
    session = _FakeLongSession(outcome=CancelRequested("job-1"))
    with pytest.raises(CancelRequested):
        await rsync_transfer(
            job=_job(),
            ssh=_FakeSshManager(session),
            source_server=_server("src"),
            target_server=_server("dst"),
            source_size_b=None,
        )
    assert session.closed  # cancel is never swallowed; the partial stays for resume


# ---- 5. LongCommandSession real cancel semantics (exemption import, see docstring) -----


@pytest.mark.asyncio
async def test_cancel_closes_channel_within_one_poll_interval() -> None:
    connection = _FakeConnection()
    session = LongCommandSession(connection, Settings())
    task = asyncio.create_task(session.run("rsync -r /s h:/d", timeout_s=30.0))
    try:
        for _ in range(200):
            if connection.processes:
                break
            await asyncio.sleep(0.01)
        assert connection.processes, "run() never created its process"
        process = connection.processes[0]
        await asyncio.sleep(0.05)  # let run() enter its poll loop
        assert not process.closed

        loop = asyncio.get_running_loop()
        started = loop.time()
        await session.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2.0)
        elapsed = loop.time() - started
        assert process.closed  # channel closed -> remote sshd terminates rsync
        assert elapsed < 1.0  # at most one 0.5s poll interval (+ CI slack)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# ---- 6. FileStat / protocol extension surface (getattr only, no connection) ------------


def test_transfer_session_protocol_declares_new_primitives() -> None:
    members = get_protocol_members(TransferSession)
    for name in ("stat", "lstat", "open_reader", "open_writer", "set_mtime", "close"):
        assert name in members


def test_filestat_carries_symlink_and_second_precision_mtime() -> None:
    stat = FileStat(exists=True, is_dir=False, is_symlink=True, mtime_s=1_700_000_000)
    assert stat.is_symlink and stat.mtime_s == 1_700_000_000
    missing = FileStat(exists=False, is_dir=False)
    assert missing.is_symlink is False and missing.mtime_s == 0


def test_sftp_transfer_session_implements_new_primitives() -> None:
    for name in ("lstat", "open_reader", "open_writer", "set_mtime"):
        method = getattr(SftpTransferSession, name, None)
        assert callable(method), name
        assert inspect.iscoroutinefunction(method), name
    reader_params = inspect.signature(SftpTransferSession.open_reader).parameters
    assert reader_params["offset"].default == 0
    writer_params = inspect.signature(SftpTransferSession.open_writer).parameters
    assert writer_params["offset"].default == 0
    assert writer_params["truncate"].default is False

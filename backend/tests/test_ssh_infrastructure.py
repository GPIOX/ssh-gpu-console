"""Behavioral tests for ManagedConnection and SshManager (no real SSH server)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.models.server import HostKeyPrompt, ServerRecord
from app.ssh.config_resolver import SSHConfigResolver
from app.ssh.connection import ManagedConnection
from app.ssh.errors import ConnectError, SshErrorCode
from app.ssh.executor import ExecutorError, RemoteCommandResult
from app.ssh.known_hosts import HostKeyTrust
from app.ssh.manager import SshManager


class _Completed:
    def __init__(self, stdout: str = "pong\n", stderr: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _GlobalTracker:
    def __init__(self) -> None:
        self.count = 0
        self.max = 0

    def enter(self) -> None:
        self.count += 1
        self.max = max(self.max, self.count)

    def leave(self) -> None:
        self.count -= 1


class _Connection:
    """Fake asyncssh client connection: counts commands and concurrency."""

    def __init__(
        self,
        delay: float = 0.0,
        started: asyncio.Event | None = None,
        tracker: _GlobalTracker | None = None,
    ) -> None:
        self.delay = delay
        self.started = started
        self.tracker = tracker
        self.closed = False
        self.aborted = False
        self.active = 0
        self.max_active = 0
        self.commands = 0

    def is_closed(self) -> bool:
        return self.closed or self.aborted

    async def run(self, _command: str, **kwargs: Any) -> _Completed:
        del kwargs  # the fake ignores the timeout; wait_for still enforces it
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.commands += 1
        if self.tracker is not None:
            self.tracker.enter()
        if self.started is not None:
            self.started.set()
        try:
            await asyncio.sleep(self.delay)
            return _Completed()
        finally:
            if self.tracker is not None:
                self.tracker.leave()
            self.active -= 1

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def abort(self) -> None:
        self.aborted = True
        self.closed = True


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(data_dir=tmp_path, **overrides)


def _empty_resolver(tmp_path: Path) -> SSHConfigResolver:
    # Missing file -> no ssh_config influence; keeps tests hermetic.
    return SSHConfigResolver(tmp_path / "ssh_config")


def _server(server_id: str, host: str | None = None) -> ServerRecord:
    name = f"srv-{server_id}"
    return ServerRecord(server_id=name, display_name=name, ssh_host=host or f"host-{server_id}")


def _manager(
    tmp_path: Path, factory: Any, trust_store: HostKeyTrust | None = None, **overrides: Any
) -> SshManager:
    settings = _settings(tmp_path, **overrides)
    return SshManager(
        settings,
        resolver=_empty_resolver(tmp_path),
        trust_store=trust_store or HostKeyTrust(tmp_path / "trusted_host_keys.json"),
        connect_factory=factory,
    )


async def test_reuses_one_connection_for_multiple_commands(tmp_path: Path) -> None:
    created: list[_Connection] = []

    async def factory(_params: Any) -> _Connection:
        connection = _Connection()
        created.append(connection)
        return connection

    manager = _manager(tmp_path, factory)
    server = _server("one")
    results = [await manager.run(server, "cmd") for _ in range(3)]
    assert all(result.exit_code == 0 for result in results)
    assert len(created) == 1
    assert created[0].commands == 3
    await manager.close_all()
    assert created[0].closed


async def test_close_server_retires_only_target_connection(tmp_path: Path) -> None:
    connections: dict[str, _Connection] = {}

    async def factory(params: Any) -> _Connection:
        connection = _Connection()
        connections[params.host] = connection
        return connection

    manager = _manager(tmp_path, factory)
    target, peer = _server("target"), _server("peer")
    await manager.run(target, "cmd")
    await manager.run(peer, "cmd")

    await manager.close_server(target.server_id)

    assert connections["host-target"].closed
    stats = await manager.connection_stats()
    assert {item["server_id"] for item in stats} == {peer.server_id}
    await manager.run(peer, "cmd")
    assert connections["host-peer"].commands == 2
    await manager.close_server(target.server_id)  # idempotent
    await manager.close_all()


async def test_close_server_rejects_queued_commands_after_retire(tmp_path: Path) -> None:
    connections: list[_Connection] = []
    started = asyncio.Event()

    async def factory(_params: Any) -> _Connection:
        connection = _Connection(delay=0.05, started=started)
        connections.append(connection)
        return connection

    manager = _manager(tmp_path, factory, max_in_flight_per_server=1)
    target = _server("queued")
    active = asyncio.create_task(manager.run(target, "cmd"))
    await started.wait()
    queued = asyncio.create_task(manager.run(target, "cmd"))
    closing = asyncio.create_task(manager.close_server(target.server_id))

    active_result = await active
    with pytest.raises(ExecutorError) as error:
        await queued
    await closing

    assert active_result.exit_code == 0
    assert error.value.code == SshErrorCode.CONNECTION_LOST.value
    assert connections[0].commands == 1
    assert connections[0].closed
    assert await manager.connection_stats() == []
    await manager.close_all()


async def test_global_and_per_server_concurrency_are_bounded(tmp_path: Path) -> None:
    connections: dict[str, _Connection] = {}
    tracker = _GlobalTracker()

    async def factory(params: Any) -> _Connection:
        connection = _Connection(delay=0.03, tracker=tracker)
        connections[params.host] = connection
        return connection

    manager = _manager(tmp_path, factory, max_in_flight_global=2, max_in_flight_per_server=1)

    async def run_one(index: int) -> None:
        await manager.run(_server(str(index % 3)), "cmd")

    await asyncio.gather(*(run_one(index) for index in range(9)))
    assert max(connection.max_active for connection in connections.values()) <= 1
    assert tracker.max <= 2  # global in-flight bound observed
    assert sum(connection.commands for connection in connections.values()) == 9
    await manager.close_all()


async def test_command_timeout_drops_connection_and_backoffs_fast(tmp_path: Path) -> None:
    async def factory(_params: Any) -> _Connection:
        return _Connection(delay=0.2)

    manager = _manager(tmp_path, factory)
    with pytest.raises(ExecutorError) as error:
        await manager.run(_server("slow"), "cmd", timeout_s=0.01)
    assert error.value.code == SshErrorCode.TIMEOUT.value
    stats = await manager.connection_stats()
    assert stats[0]["open"] is False
    assert stats[0]["in_backoff"] is True

    # The next run fails fast, before queueing on the semaphores.
    started_at = time.monotonic()
    with pytest.raises(ExecutorError) as backoff:
        await manager.run(_server("slow"), "cmd")
    assert backoff.value.code == SshErrorCode.RECONNECT_BACKOFF.value
    assert time.monotonic() - started_at < 0.5
    await manager.close_all()


async def test_authentication_failure_is_classified_and_not_scheduled(tmp_path: Path) -> None:
    attempts = {"count": 0}

    async def factory(_params: Any) -> _Connection:
        attempts["count"] += 1
        raise RuntimeError("Permission denied (publickey)")  # auth marker message

    manager = _manager(tmp_path, factory)
    result = await manager.test_connection(_server("auth"))
    assert result.ok is False
    assert result.status == "authentication_failed"
    assert attempts["count"] == 1
    stats = await manager.connection_stats()
    assert stats[0]["in_backoff"] is False  # non-retryable: no backoff scheduled
    with pytest.raises(ExecutorError) as error:
        await manager.run(_server("auth"), "cmd")
    assert error.value.code == "authentication_failed"
    await manager.close_all()


async def test_connect_timeout_is_classified(tmp_path: Path) -> None:
    async def factory(_params: Any) -> _Connection:
        raise TimeoutError("connection to host timed out")

    manager = _manager(tmp_path, factory)
    result = await manager.test_connection(_server("slow-host"))
    assert result.ok is False
    assert result.status == "timeout"
    await manager.close_all()


async def test_one_offline_server_does_not_block_another(tmp_path: Path) -> None:
    async def factory(params: Any) -> _Connection:
        if params.host == "host-offline":
            raise ConnectionRefusedError("offline")
        return _Connection()

    manager = _manager(tmp_path, factory)
    offline, online = await asyncio.gather(
        manager.run(_server("offline"), "cmd"),
        manager.run(_server("online"), "cmd"),
        return_exceptions=True,
    )
    assert isinstance(offline, ExecutorError)
    assert offline.code == "connect_failed"
    assert isinstance(online, RemoteCommandResult)
    assert online.exit_code == 0
    await manager.close_all()


async def test_identity_change_recreates_connection(tmp_path: Path) -> None:
    connections: dict[str, _Connection] = {}

    async def factory(params: Any) -> _Connection:
        connection = _Connection()
        connections[params.host] = connection
        return connection

    manager = _manager(tmp_path, factory)
    server = _server("move", host="host-a")
    await manager.run(server, "cmd")
    await manager.run(_server("move", host="host-b"), "cmd")
    assert connections["host-a"].closed
    assert connections["host-b"].commands == 1
    stats = await manager.connection_stats()
    assert {item["host"] for item in stats} == {"host-b"}
    await manager.close_all()


async def test_note_connection_lost_ignores_unknown_server(tmp_path: Path) -> None:
    connections: dict[str, _Connection] = {}

    async def factory(params: Any) -> _Connection:
        connection = _Connection()
        connections[params.host] = connection
        return connection

    manager = _manager(tmp_path, factory)

    # Unknown server: no entry may be created.
    await manager.note_connection_lost("no-such-server", "stale notification")
    assert await manager.connection_stats() == []

    # Known server: dropped and scheduled for backoff.
    await manager.run(_server("known"), "cmd")
    await manager.note_connection_lost("srv-known", "boom")
    stats = await manager.connection_stats()
    assert stats[0]["open"] is False
    assert stats[0]["in_backoff"] is True
    await manager.close_all()


async def test_unknown_host_key_surfaces_pending_prompt_then_trust(tmp_path: Path) -> None:
    trust = HostKeyTrust(tmp_path / "trusted_host_keys.json")
    prompt = HostKeyPrompt(
        host="host-new", port=22, key_type="ssh-ed25519", fingerprint="SHA256:abc123"
    )

    async def factory(params: Any) -> _Connection:
        if trust.is_trusted(params.original_host or params.host, params.port, prompt.fingerprint):
            return _Connection()
        raise ConnectError(
            SshErrorCode.HOST_KEY_UNKNOWN, "host key is not trusted", pending_host_key=prompt
        )

    manager = _manager(tmp_path, factory, trust_store=trust)
    server = _server("new")

    result = await manager.test_connection(server)
    assert result.ok is False
    assert result.status == "host_key_error"
    assert result.pending_host_key is not None
    assert result.pending_host_key.fingerprint == "SHA256:abc123"

    wrong = HostKeyPrompt(
        host="host-new", port=22, key_type="ssh-ed25519", fingerprint="SHA256:WRONG"
    )
    rejected = await manager.trust_host_key(server, wrong)
    assert rejected.ok is False
    assert not trust.has_host("host-new", 22)

    trusted = await manager.trust_host_key(server, prompt)
    assert trusted.ok is True
    assert trusted.status == "online"
    assert trust.is_trusted("host-new", 22, "SHA256:abc123")
    await manager.close_all()


async def test_mismatch_is_distinct_from_unknown_and_has_no_trust_path(tmp_path: Path) -> None:
    reasons = iter(
        (
            ConnectError(SshErrorCode.HOST_KEY_UNKNOWN, "host key is not trusted"),
            ConnectError(
                SshErrorCode.HOST_KEY_MISMATCH, "host key mismatch: possible MITM", retryable=False
            ),
        )
    )

    async def factory(_params: Any) -> _Connection:
        raise next(reasons)

    manager = _manager(tmp_path, factory)
    unknown = await manager.test_connection(_server("fresh"))
    assert unknown.status == "host_key_error"
    assert unknown.pending_host_key is None  # fake factory sent no prompt this time

    mismatch = await manager.test_connection(_server("evil"))
    assert mismatch.status == "host_key_error"
    assert mismatch.pending_host_key is None  # mismatch never offers a trust path
    trust_attempt = await manager.trust_host_key(
        _server("evil"),
        HostKeyPrompt(host="host-evil", port=22, key_type="ssh-ed25519", fingerprint="SHA256:x"),
    )
    assert trust_attempt.ok is False
    await manager.close_all()


async def test_cancelled_command_propagates_and_keeps_connection(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def factory(_params: Any) -> _Connection:
        return _Connection(delay=5.0, started=started)

    manager = _manager(tmp_path, factory)
    task = asyncio.create_task(manager.run(_server("cancel"), "cmd"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stats = await manager.connection_stats()
    assert stats[0]["open"] is True  # cancellation is not a transport failure
    await manager.close_all()


async def test_backoff_delay_is_bounded_and_grows(tmp_path: Path) -> None:
    async def factory(_params: Any) -> _Connection:
        raise AssertionError("must not connect")

    connection = ManagedConnection("srv", _settings(tmp_path), factory)
    delays: list[float] = []
    for _ in range(6):
        connection.attempts += 1
        delays.append(connection.backoff_delay())
    assert all(2.0 <= delay <= 60.0 for delay in delays)
    assert delays[2] > delays[0] * 1.5  # exponential growth dominates the jitter
    connection.next_attempt_at = time.monotonic() + 3.0
    assert connection.in_backoff()
    assert 2.0 < connection.backoff_remaining() <= 3.0


# ---- SftpTransferSession exception mapping ------------------------------------------


def test_sftp_transfer_session_treats_sftp_no_such_file_as_missing() -> None:
    """asyncssh raises SFTPNoSuchFile (message 'No such file'), which is NOT a
    builtin FileNotFoundError/OSError subclass. stat()/mkdir() must map it to
    'missing' instead of letting it kill a transfer (observed in the field:
    the first missing parent on the target killed a job at 0 bytes)."""
    from app.ssh.transport import SftpTransferSession
    from asyncssh.sftp import SFTPNoSuchFile

    existing = {"/home", "/home/user"}

    class FakeSftp:
        def __init__(self) -> None:
            self.made: list[str] = []

        async def stat(self, path: str) -> Any:
            if path in existing:
                return type("Attrs", (), {"permissions": 0o40755, "size": 4096})()
            raise SFTPNoSuchFile(2, "No such file")

        async def mkdir(self, path: str) -> None:
            if path in existing:
                raise FileExistsError(path)
            existing.add(path)
            self.made.append(path)

    fake = FakeSftp()
    session = SftpTransferSession(None, fake, Settings())  # type: ignore[arg-type]

    info = asyncio.run(session.stat("/home/user"))
    assert info.exists and info.is_dir

    missing = asyncio.run(session.stat("/home/user/missing"))
    assert missing.exists is False and missing.is_dir is False

    # Creates ONLY the missing levels; existing ones are neither created nor
    # re-created; existing dirs reported by the server are accepted.
    asyncio.run(session.mkdir("/home/user/new/tree"))
    assert fake.made == ["/home/user/new", "/home/user/new/tree"]

    assert asyncio.run(session.stat("/home/user")).exists is True


def test_sftp_transfer_session_rename_overwrites_existing_target() -> None:
    """Plain SFTP rename fails on an existing destination ('Failure' — seen on
    every retry over a previously copied tree). posix-rename must be used when
    the server offers it, with an explicit replace fallback otherwise."""
    from app.ssh.transport import SftpTransferSession
    from asyncssh.sftp import SFTPNoSuchFile

    class _PosixSftp:
        def __init__(self) -> None:
            self.posix_calls: list[tuple[str, str]] = []
            self.removed: list[str] = []
            self.renamed: list[tuple[str, str]] = []

        async def posix_rename(self, source: str, target: str) -> None:
            self.posix_calls.append((source, target))

        async def remove(self, path: str) -> None:
            self.removed.append(path)

        async def rename(self, source: str, target: str) -> None:
            self.renamed.append((source, target))

    fake = _PosixSftp()
    session = SftpTransferSession(None, fake, Settings())  # type: ignore[arg-type]
    asyncio.run(session.rename("/a/partial", "/a/final"))
    assert fake.posix_calls == [("/a/partial", "/a/final")]
    assert fake.removed == [] and fake.renamed == []

    class _NoPosixSftp:
        # deliberately lacks posix_rename: attribute access raises AttributeError
        def __init__(self) -> None:
            self.removed: list[str] = []
            self.renamed: list[tuple[str, str]] = []
            self.target_exists = True

        async def remove(self, path: str) -> None:
            if not self.target_exists:
                raise SFTPNoSuchFile(2, "No such file")
            self.removed.append(path)

        async def rename(self, source: str, target: str) -> None:
            self.renamed.append((source, target))

    fake2 = _NoPosixSftp()
    session2 = SftpTransferSession(None, fake2, Settings())  # type: ignore[arg-type]
    asyncio.run(session2.rename("/a/partial", "/a/target"))
    assert fake2.removed == ["/a/target"]
    assert fake2.renamed == [("/a/partial", "/a/target")]

    fake3 = _NoPosixSftp()
    fake3.target_exists = False
    session3 = SftpTransferSession(None, fake3, Settings())  # type: ignore[arg-type]
    asyncio.run(session3.rename("/a/partial", "/a/fresh"))
    assert fake3.removed == []
    assert fake3.renamed == [("/a/partial", "/a/fresh")]

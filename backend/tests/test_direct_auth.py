"""Server→server direct-transfer auth tests (Phase 4.2C; offline fakes, zero SSH).

Exemption basis (backend/AGENTS.md normally forbids importing the ssh transport
modules in tests): mirroring the Sol-approved exemption in test_rsync_security.py,
THIS file imports `app.ssh.transport` for offline unit tests only — to pin the
pure-data `VerifiedHostKey` surface and `SftpTransferSession.set_mode`. Every
remote machine is an in-memory RemoteFS with scripted executors; key generation
is simulated by the fake manager. No network, no real hostnames.

Spec items covered: native priority (37), dedicated setup transaction (27),
idempotent duplicate setup (28), failure injection + rollback, revoke exactness,
planner integration (25-26), and the forbidden-string repo scan (41).
"""

from __future__ import annotations

import base64
import posixpath
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.direct_auth.metadata import DirectAuthMetadata
from app.direct_auth.service import (
    DirectAuthService,
    _classify_probe_failure,
    _openssh_fingerprint,
)
from app.models.direct_auth import DirectAuthPairMetadata
from app.models.transfer import DedicatedKeyOptions, TransferStrategy
from app.persistence.json_store import JsonFileStore
from app.servers.registry import ServerRegistry, set_default_registry
from app.ssh.executor import ExecutorError
from app.ssh.file_transfer import FileStat
from app.ssh.transport import SftpTransferSession, VerifiedHostKey, verified_server_host_key
from app.transfer.planner import build_rsync_command, dedicated_batch_mode_probe, plan_transfer
from fastapi import FastAPI
from fastapi.testclient import TestClient

# A deterministic SYNTHETIC public-key blob (valid base64; never a real key).
_PUB_BLOB = base64.b64encode(b"sgc-synthetic-key-blob-0001" + bytes(range(28))).decode("ascii")
_PUB_LINE = f"ssh-ed25519 {_PUB_BLOB}"

SRC = "srv-src"
DST = "srv-dst"
OTHER = "srv-other"
TARGET_HOST = "target-node.example"
TARGET_PORT = 2201
TARGET_USER = "opsuser"
USER_AUTH_KEYS = b"ssh-rsa AAAAUSERKEYONE alpha@old\nssh-ed25519 AAAAUSERKEYTWO beta@host\n"
PRIVATE_PATH = ".ssh/gpu-console/keys/sgc-direct-srv-src-srv-dst"
KNOWN_HOSTS_PATH = ".ssh/gpu-console/known_hosts"

_DEDICATED = DedicatedKeyOptions(
    private_key_path=PRIVATE_PATH,
    known_hosts_path=KNOWN_HOSTS_PATH,
)


def _verified_host_key_stub(service: DirectAuthService) -> None:
    async def fake_verified(server: object) -> VerifiedHostKey:
        del server
        return VerifiedHostKey(
            key_type="ssh-ed25519",
            openssh_public_key_line=_PUB_LINE,
            fingerprint="SHA256:verified",
        )

    service._verified_host_key = fake_verified  # type: ignore[method-assign]


# ---- in-memory remote machine ----------------------------------------------------------


class RemoteFS:
    """One fake remote machine: dirs with modes, files, injection switches."""

    def __init__(self, server_id: str) -> None:
        self.server_id = server_id
        self.dirs: dict[str, int] = {}
        self.files: dict[str, bytes] = {}
        self.modes: dict[str, int] = {}
        self.mode_intents: list[tuple[str, int]] = []
        self.removed: list[str] = []
        self.fail_writes = 0  # the next N open_writer calls raise
        self.fail_reads = False  # every open_reader raises


class _FakeReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    async def read(self, size: int) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


class _FakeWriter:
    def __init__(self, path: str, fs: RemoteFS) -> None:
        self._path = path
        self._fs = fs
        self._buffer = bytearray()

    async def write(self, data: bytes) -> None:
        self._buffer.extend(data)

    async def close(self) -> None:
        self._fs.files[self._path] = bytes(self._buffer)


class FakeSftpSession:
    """TransferSession double over RemoteFS; records 0700/0600 intentions."""

    def __init__(self, fs: RemoteFS) -> None:
        self._fs = fs
        self.closed = False

    async def stat(self, path: str) -> FileStat:
        if path in self._fs.files:
            return FileStat(exists=True, is_dir=False, size_b=len(self._fs.files[path]))
        if path in self._fs.dirs or path == "/":
            return FileStat(exists=True, is_dir=True)
        return FileStat(exists=False, is_dir=False)

    async def lstat(self, path: str) -> FileStat:
        return await self.stat(path)

    async def mkdir(self, path: str) -> None:
        parts = posixpath.normpath(path).strip("/").split("/")
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            if current in self._fs.dirs:
                continue
            if current in self._fs.files:
                raise FileExistsError(current)
            self._fs.dirs[current] = 0o755

    async def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names = []
        for entry in list(self._fs.files) + list(self._fs.dirs):
            if entry.startswith(prefix) and "/" not in entry[len(prefix) :]:
                names.append(entry[len(prefix) :])
        return names

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        if self._fs.fail_reads:
            raise OSError("injected read failure")
        if path not in self._fs.files:
            raise FileNotFoundError(path)
        return _FakeReader(self._fs.files[path][offset:])

    async def open_writer(self, path: str, *, offset: int = 0, truncate: bool = False) -> Any:
        if self._fs.fail_writes > 0:
            self._fs.fail_writes -= 1
            raise OSError("injected write failure")
        return _FakeWriter(path, self._fs)

    async def set_mtime(self, path: str, mtime_s: int) -> None:
        del path, mtime_s

    async def set_mode(self, path: str, mode: int) -> None:
        self._fs.mode_intents.append((path, mode))
        self._fs.modes[path] = mode

    async def rename(self, source: str, target: str) -> None:
        if source not in self._fs.files:
            raise FileNotFoundError(source)
        self._fs.files[target] = self._fs.files.pop(source)
        if source in self._fs.modes:
            self._fs.modes[target] = self._fs.modes.pop(source)

    async def remove(self, path: str) -> None:
        if path not in self._fs.files:
            raise FileNotFoundError(path)
        self._fs.files.pop(path)
        self._fs.modes.pop(path, None)
        self._fs.removed.append(path)

    async def close(self) -> None:
        self.closed = True


class FakeLongSession:
    """Duck LongCommandSession: probe outcomes scripted FIFO by the manager."""

    def __init__(self, manager: FakeSshManager, server_id: str) -> None:
        self.commands: list[str] = []
        self.closed = False
        self._manager = manager
        self._server_id = server_id

    async def run(
        self, command: str, *, timeout_s: float, on_stdout: Any = None, on_stderr: Any = None
    ) -> int:
        self.commands.append(command)
        exit_code, stderr = self._manager.next_probe_result()
        if on_stderr is not None and stderr:
            await on_stderr(stderr)
        return exit_code

    async def close(self) -> None:
        self.closed = True


class FakeSshManager:
    """Duck SshManager: simulates both executors and SFTP for two machines."""

    def __init__(self, *, rsync_ok: bool = True, ssh_keygen_ok: bool = True) -> None:
        self.fs: dict[str, RemoteFS] = {}
        self.exec_log: list[tuple[str, str]] = []
        self.exec_failures: dict[str, ExecutorError] = {}
        self.rsync_ok = rsync_ok
        self.ssh_keygen_ok = ssh_keygen_ok
        self.sftp_opens: list[str] = []
        self.closed_servers: list[str] = []
        self.command_sessions: list[FakeLongSession] = []
        self._probe_results: list[tuple[int, str]] = []

    def queue_probe(self, exit_code: int, stderr: str = "") -> None:
        self._probe_results.append((exit_code, stderr))

    def next_probe_result(self) -> tuple[int, str]:
        if not self._probe_results:
            raise AssertionError("probe command without a scripted outcome")
        return self._probe_results.pop(0)

    def fs_for(self, server: Any) -> RemoteFS:
        fs = self.fs.get(server.server_id)
        if fs is None:
            fs = RemoteFS(server.server_id)
            self.fs[server.server_id] = fs
        return fs

    async def run(self, server: Any, command: str, *, timeout_s: float | None = None) -> Any:
        self.exec_log.append((server.server_id, command))
        failure = self.exec_failures.get(server.server_id)
        if failure is not None:
            raise failure
        exit_code, stdout, stderr = self._dispatch(server, command)
        return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_ms=1.0)

    def resolve_params_for(self, server: Any) -> Any:
        return SimpleNamespace(
            host=server.ssh_host, port=server.port or 22, username=server.username
        )

    async def close_server(self, server_id: str) -> None:
        self.closed_servers.append(server_id)

    async def transfer_session(self, server: Any) -> FakeSftpSession:
        self.sftp_opens.append(server.server_id)
        return FakeSftpSession(self.fs_for(server))

    async def transfer_command_session(self, server: Any) -> FakeLongSession:
        session = FakeLongSession(self, server.server_id)
        self.command_sessions.append(session)
        return session

    def _dispatch(self, server: Any, command: str) -> tuple[int, str, str]:
        if command == "true":
            return (0, "", "")
        if command == "command -v rsync":
            if self.rsync_ok:
                return (0, "/usr/bin/rsync\n", "")
            return (127, "", "not found")
        if command == "command -v ssh-keygen":
            if self.ssh_keygen_ok:
                return (0, "/usr/bin/ssh-keygen\n", "")
            return (1, "", "")
        if command.startswith("ssh-keygen "):
            return self._fake_keygen(server, command)
        if command.startswith("cat "):
            path = shlex.split(command)[1]
            fs = self.fs_for(server)
            if path not in fs.files:
                return (1, "", f"cat: {path}: No such file or directory")
            return (0, fs.files[path].decode("utf-8"), "")
        if command.startswith("rm -f"):
            tokens = shlex.split(command)
            fs = self.fs_for(server)
            for path in tokens[tokens.index("--") + 1 :]:
                fs.files.pop(path, None)
                fs.modes.pop(path, None)
                fs.removed.append(path)
            return (0, "", "")
        raise AssertionError(f"unexpected fixed command: {command!r}")

    def _fake_keygen(self, server: Any, command: str) -> tuple[int, str, str]:
        tokens = shlex.split(command)
        private = tokens[tokens.index("-f") + 1]
        comment = tokens[tokens.index("-C") + 1]
        fs = self.fs_for(server)
        fs.files[private] = b"SYNTHETIC PRIVATE KEY"
        fs.files[f"{private}.pub"] = f"ssh-ed25519 {_PUB_BLOB} {comment}\n".encode("ascii")
        fs.modes[private] = 0o600
        return (0, "", "")


# ---- harness --------------------------------------------------------------------------


def _record(server_id: str, *, host: str, port: int, username: str, enabled: bool = True) -> Any:
    return SimpleNamespace(
        server_id=server_id,
        display_name=server_id,
        ssh_host=host,
        username=username,
        port=port,
        enabled=enabled,
    )


def _servers() -> dict[str, Any]:
    return {
        SRC: _record(SRC, host="source-node.example", port=2222, username="deploysrc"),
        DST: _record(DST, host=TARGET_HOST, port=TARGET_PORT, username=TARGET_USER),
        OTHER: _record(OTHER, host="other-node.example", port=22, username="misc"),
    }


def _service(tmp_path: Path, manager: FakeSshManager, servers: dict[str, Any]) -> DirectAuthService:
    metadata = DirectAuthMetadata(JsonFileStore(tmp_path / "direct_auth.json"))
    return DirectAuthService(
        Settings(data_dir=tmp_path),
        manager,  # type: ignore[arg-type]
        servers.get,
        lambda server_id: server_id in servers,
        metadata=metadata,
    )


def _stored_record(source: str, target: str, key_id: str) -> DirectAuthPairMetadata:
    return DirectAuthPairMetadata(
        source_server_id=source,
        target_server_id=target,
        key_id=key_id,
        remote_private_key_path=f".ssh/gpu-console/keys/sgc-direct-{source}-{target}",
        remote_public_key_path=f".ssh/gpu-console/keys/sgc-direct-{source}-{target}.pub",
        remote_known_hosts_path=KNOWN_HOSTS_PATH,
        public_key_fingerprint=_openssh_fingerprint(_PUB_BLOB),
        created_at="2026-01-01T00:00:00+00:00",
    )


# ---- 1. explicit check: native first, never touching key material -------------------------


async def test_check_native_available_no_key_actions(tmp_path: Path) -> None:
    manager = FakeSshManager()
    service = _service(tmp_path, manager, _servers())
    manager.queue_probe(0, "")

    status = await service.check(SRC, DST)

    assert status.available is True
    assert status.method == "native"
    assert status.configured is False
    assert status.reason is None
    assert status.checked_at is not None
    assert manager.sftp_opens == []
    assert not any(cmd.startswith("ssh-keygen") for _, cmd in manager.exec_log)
    probe = manager.command_sessions[0].commands[0]
    assert probe == (
        "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes"
        f" -p {TARGET_PORT} {TARGET_USER}@{TARGET_HOST} true"
    )


async def test_check_native_failure_classifies_from_stderr(tmp_path: Path) -> None:
    manager = FakeSshManager()
    service = _service(tmp_path, manager, _servers())
    manager.queue_probe(255, "Permission denied (publickey,password).\r\n")

    status = await service.check(SRC, DST)

    assert status.available is False
    assert status.method is None
    assert status.configured is False
    assert status.reason == "authentication_failed"


async def test_check_rsync_missing_source_gates_before_probe(tmp_path: Path) -> None:
    manager = FakeSshManager(rsync_ok=False)
    service = _service(tmp_path, manager, _servers())

    status = await service.check(SRC, DST)

    assert status.reason == "rsync_missing_source"
    assert status.available is False
    assert manager.command_sessions == []


async def test_check_dedicated_pair_uses_its_own_probe(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    service._metadata.put(_stored_record(SRC, DST, "kid77"))
    manager.queue_probe(255, "Permission denied (publickey).")  # native probe fails
    manager.queue_probe(0, "")  # dedicated probe passes

    status = await service.check(SRC, DST)

    assert status.configured is True
    assert status.method == "sgc_key"
    assert status.available is True
    probe = manager.command_sessions[1].commands[0]  # each probe opens its own session
    assert f"-i {PRIVATE_PATH}" in probe
    assert f"-o UserKnownHostsFile={KNOWN_HOSTS_PATH}" in probe
    assert "-o IdentitiesOnly=yes" in probe


async def test_check_dedicated_route_failure_keeps_method(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    service._metadata.put(_stored_record(SRC, DST, "kid77"))
    manager.queue_probe(255, "Permission denied (publickey).")  # native probe fails
    refused = f"ssh: connect to host {TARGET_HOST} port {TARGET_PORT}: Connection refused"
    manager.queue_probe(255, refused)  # dedicated probe cannot connect

    status = await service.check(SRC, DST)

    assert status.configured is True
    assert status.method == "sgc_key"
    assert status.available is False
    assert status.reason == "route_unreachable"


def test_failure_classifier_maps_spec_markers() -> None:
    assert (
        _classify_probe_failure("Load key '/k': No such file or directory", dedicated=True)
        == "dedicated_key_missing"
    )
    assert (
        _classify_probe_failure(
            "Warning: Identity file /k not accessible, reticulating", dedicated=True
        )
        == "dedicated_key_missing"
    )
    assert (
        _classify_probe_failure("Permission denied (publickey).", dedicated=False)
        == "authentication_failed"
    )
    assert (
        _classify_probe_failure("Host key verification failed.", dedicated=False)
        == "host_key_mismatch"
    )
    assert (
        _classify_probe_failure(
            "No ED25519 host key is known for h and you have requested strict checking",
            dedicated=True,
        )
        == "host_key_unknown"
    )
    assert (
        _classify_probe_failure("ssh: Could not resolve hostname h", dedicated=False)
        == "route_unreachable"
    )
    assert _classify_probe_failure("mystery failure", dedicated=True) == "unknown"


# ---- 2. dedicated setup happy path (spec items 27-28) --------------------------------------


async def test_setup_happy_path_installs_everything_once(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    dst_fs = manager.fs_for(servers[DST])
    dst_fs.files[".ssh/authorized_keys"] = USER_AUTH_KEYS
    src_fs = manager.fs_for(servers[SRC])
    src_fs.files[".ssh/known_hosts"] = b"# user's own file: never touched\n"
    manager.queue_probe(0, "")  # final dedicated check

    status = await service.setup_key(SRC, DST)

    assert status.configured is True
    assert status.available is True
    assert status.method == "sgc_key"

    # Directory + permission intentions.
    intents = dict(src_fs.mode_intents)
    assert intents[".ssh/gpu-console"] == 0o700
    assert intents[".ssh/gpu-console/keys"] == 0o700
    assert intents[".ssh"] == 0o700  # created by us
    assert dict(dst_fs.mode_intents)[".ssh"] == 0o700

    # authorized_keys: user lines preserved byte-for-byte, ONE restricted line.
    authorized = dst_fs.files[".ssh/authorized_keys"]
    assert dst_fs.modes[".ssh/authorized_keys"] == 0o600
    lines = authorized.decode().splitlines()
    assert lines[:2] == ["ssh-rsa AAAAUSERKEYONE alpha@old", "ssh-ed25519 AAAAUSERKEYTWO beta@host"]
    restricted = [
        line
        for line in lines
        if line.startswith("no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty ")
    ]
    assert len(restricted) == 1
    key_id = restricted[0].rsplit(":", 1)[-1]
    assert restricted[0] == (
        f"no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty"
        f" ssh-ed25519 {_PUB_BLOB} sgc-direct:{SRC}:{DST}:{key_id}"
    )

    # Fixed keygen command, run exactly once; public key read via fixed `cat`.
    executed = [cmd for server_id, cmd in manager.exec_log if server_id == SRC]
    assert keygen_commands(executed) == [
        f"ssh-keygen -t ed25519 -N '' -C sgc-direct:{SRC}:{DST}:{key_id} -f {PRIVATE_PATH}"
    ]
    assert cat_commands(executed) == [f"cat {PRIVATE_PATH}.pub"]
    assert f"cat {PRIVATE_PATH}" not in executed  # the private key is NEVER read

    # App-owned known_hosts receives the verified key with [host]:port notation.
    known_hosts = src_fs.files[KNOWN_HOSTS_PATH]
    assert src_fs.modes[KNOWN_HOSTS_PATH] == 0o600
    assert known_hosts.decode().splitlines() == [
        f"[{TARGET_HOST}]:{TARGET_PORT} ssh-ed25519 {_PUB_BLOB}"
    ]

    # The final dedicated check command (pinned shape).
    assert manager.command_sessions[0].commands == [
        f"ssh -i {PRIVATE_PATH}"
        " -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
        f" -o UserKnownHostsFile={KNOWN_HOSTS_PATH} -o ConnectTimeout=8"
        f" -p {TARGET_PORT} {TARGET_USER}@{TARGET_HOST} true"
    ]

    # Metadata persisted ONLY after success, fingerprint matching the blob.
    record = service._metadata.get(SRC, DST)
    assert record is not None
    assert record.key_id == key_id
    assert record.remote_private_key_path == PRIVATE_PATH
    assert record.public_key_fingerprint == _openssh_fingerprint(_PUB_BLOB)
    assert (tmp_path / "direct_auth.json").is_file()

    # The user's own known_hosts on the source is unchanged.
    assert src_fs.files[".ssh/known_hosts"] == b"# user's own file: never touched\n"
    assert src_fs.mode_intents.count((".ssh/known_hosts", 0o600)) == 0


def keygen_commands(executed: list[str]) -> list[str]:
    return [cmd for cmd in executed if cmd.startswith("ssh-keygen ")]


def cat_commands(executed: list[str]) -> list[str]:
    return [cmd for cmd in executed if cmd.startswith("cat ")]


async def test_duplicate_setup_is_idempotent(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    dst_fs = manager.fs_for(servers[DST])
    manager.queue_probe(0, "")
    first = await service.setup_key(SRC, DST)
    assert first.available is True

    content_after_first = dst_fs.files[".ssh/authorized_keys"]
    record_after_first = service._metadata.get(SRC, DST)
    assert record_after_first is not None
    probe_calls = len(manager.command_sessions)

    manager.queue_probe(0, "")  # the already-configured dedicated check
    second = await service.setup_key(SRC, DST)

    assert second.configured is True
    assert second.available is True
    assert second.method == "sgc_key"
    assert second.reason == "already_configured"
    assert len(keygen_commands([c for s, c in manager.exec_log if s == SRC])) == 1
    assert dst_fs.files[".ssh/authorized_keys"] == content_after_first
    assert service._metadata.get(SRC, DST) is record_after_first
    assert len(manager.command_sessions) == probe_calls + 1


async def test_setup_port_22_known_hosts_entry_has_no_brackets(tmp_path: Path) -> None:
    servers = _servers()
    servers[DST] = _record(DST, host=TARGET_HOST, port=22, username=TARGET_USER)
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    manager.queue_probe(0, "")

    status = await service.setup_key(SRC, DST)

    assert status.available is True
    entry = manager.fs_for(servers[SRC]).files[KNOWN_HOSTS_PATH].decode().splitlines()
    assert entry == [f"{TARGET_HOST} ssh-ed25519 {_PUB_BLOB}"]


# ---- 3. failure injections: never configured, own materials rolled back --------------------
_USER_AUTH = b"ssh-rsa AAAAUSERKEYONE alpha@old\n"


async def _setup_with_failure(
    tmp_path: Path, manager: FakeSshManager, servers: dict[str, Any]
) -> DirectAuthService:
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    manager.fs_for(servers[DST]).files[".ssh/authorized_keys"] = _USER_AUTH
    manager.queue_probe(0, "")  # consumed only if the final check is reached
    await service.setup_key(SRC, DST)
    return service


async def test_setup_source_unreachable_writes_nothing(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    manager.exec_failures[SRC] = ExecutorError("connect_failed", "no route to source")
    service = await _setup_with_failure(tmp_path, manager, servers)

    status = service._latest_checks[(SRC.casefold(), DST.casefold())]
    assert status.configured is False
    assert status.available is False
    assert status.reason == "route_unreachable"
    assert service._metadata.get(SRC, DST) is None
    assert manager.fs_for(servers[SRC]).files == {}
    assert manager.fs_for(servers[DST]).files == {".ssh/authorized_keys": _USER_AUTH}


async def test_setup_target_unreachable_writes_nothing(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    manager.exec_failures[DST] = ExecutorError("connect_failed", "no route to target")
    service = await _setup_with_failure(tmp_path, manager, servers)

    status = service._latest_checks[(SRC.casefold(), DST.casefold())]
    assert status.reason == "route_unreachable"
    assert service._metadata.get(SRC, DST) is None
    assert not any("sgc-direct" in name for name in manager.fs_for(servers[SRC]).files)


async def test_setup_keygen_missing_aborts_before_material(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager(ssh_keygen_ok=False)
    service = await _setup_with_failure(tmp_path, manager, servers)

    status = service._latest_checks[(SRC.casefold(), DST.casefold())]
    assert status.configured is False
    assert status.reason == "keygen_missing_source"
    src_fs = manager.fs_for(servers[SRC])
    assert not any("sgc-direct" in name for name in src_fs.files)
    assert service._metadata.get(SRC, DST) is None


async def test_setup_authorized_write_failure_rolls_back(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    manager.fs_for(servers[DST]).fail_writes = 1  # authorized_keys temp write fails
    service = await _setup_with_failure(tmp_path, manager, servers)

    status = service._latest_checks[(SRC.casefold(), DST.casefold())]
    assert status.configured is False
    assert status.available is False
    assert status.reason == "unknown"
    src_fs = manager.fs_for(servers[SRC])
    assert not any("sgc-direct" in name for name in src_fs.files)
    dst_fs = manager.fs_for(servers[DST])
    assert dst_fs.files[".ssh/authorized_keys"] == _USER_AUTH  # user content untouched
    assert service._metadata.get(SRC, DST) is None


async def test_setup_known_hosts_failure_rolls_back_line(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    manager.fs_for(servers[SRC]).fail_writes = 1  # known_hosts temp write fails
    service = await _setup_with_failure(tmp_path, manager, servers)

    status = service._latest_checks[(SRC.casefold(), DST.casefold())]
    assert status.configured is False
    assert status.reason == "unknown"
    src_fs = manager.fs_for(servers[SRC])
    assert not any("sgc-direct" in name for name in src_fs.files)
    assert KNOWN_HOSTS_PATH not in src_fs.files
    dst_fs = manager.fs_for(servers[DST])
    assert dst_fs.files[".ssh/authorized_keys"] == _USER_AUTH  # our line was removed again
    assert service._metadata.get(SRC, DST) is None


async def test_setup_final_check_failure_classifies_and_rolls_back(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    manager.fs_for(servers[DST]).files[".ssh/authorized_keys"] = _USER_AUTH
    manager.queue_probe(255, "Permission denied (publickey).")  # final check fails

    status = await service.setup_key(SRC, DST)

    assert status.configured is False
    assert status.available is False
    assert status.reason == "authentication_failed"
    src_fs = manager.fs_for(servers[SRC])
    assert not any("sgc-direct" in name for name in src_fs.files)
    assert (
        KNOWN_HOSTS_PATH not in src_fs.files
        or src_fs.files[KNOWN_HOSTS_PATH].strip() == b""  # our entry rolled back
    )
    assert manager.fs_for(servers[DST]).files[".ssh/authorized_keys"] == _USER_AUTH
    assert service._metadata.get(SRC, DST) is None


# ---- 4. already-configured repair path ------------------------------------------------------


async def test_setup_with_broken_pair_repairs_without_orphans(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    old = _stored_record(SRC, DST, "oldkid")
    service._metadata.put(old)
    src_fs = manager.fs_for(servers[SRC])
    src_fs.files[old.remote_private_key_path] = b"old-private"
    src_fs.files[old.remote_public_key_path] = b"old-public"
    dst_fs = manager.fs_for(servers[DST])
    dst_fs.files[".ssh/authorized_keys"] = (
        "no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty"
        f" ssh-ed25519 {_PUB_BLOB} sgc-direct:{SRC}:{DST}:oldkid\n"
    ).encode("ascii")
    manager.queue_probe(255, "Permission denied (publickey).")  # broken-pair check
    manager.queue_probe(0, "")  # fresh final check

    status = await service.setup_key(SRC, DST)

    assert status.configured is True
    assert status.available is True
    record = service._metadata.get(SRC, DST)
    assert record is not None
    assert record.key_id != "oldkid"
    assert record.public_key_fingerprint == _openssh_fingerprint(_PUB_BLOB)
    sgc_lines = [
        line
        for line in dst_fs.files[".ssh/authorized_keys"].decode().splitlines()
        if "sgc-direct:" in line
    ]
    assert len(sgc_lines) == 1
    assert sgc_lines[0].endswith(f"sgc-direct:{SRC}:{DST}:{record.key_id}")
    assert len(service._metadata.for_server(SRC)) == 1  # one row, no orphans
    assert not any("oldkid" in name for name in src_fs.files)


# ---- 5. revoke -------------------------------------------------------------------------------


async def test_revoke_removes_only_this_pair(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    dst_fs = manager.fs_for(servers[DST])
    dst_fs.files[".ssh/authorized_keys"] = _USER_AUTH
    manager.queue_probe(0, "")
    assert (await service.setup_key(SRC, DST)).available is True
    ours = service._metadata.get(SRC, DST)
    assert ours is not None
    # A second SGC pair (other target) plus its metadata must survive.
    dst_fs.files[".ssh/authorized_keys"] = dst_fs.files[".ssh/authorized_keys"] + (
        "no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty"
        f" ssh-ed25519 {_PUB_BLOB} sgc-direct:{SRC}:{OTHER}:zz9\n"
    ).encode("ascii")
    service._metadata.put(_stored_record(SRC, OTHER, "zz9"))

    result = await service.revoke(SRC, DST)

    assert result.configured is False
    content = dst_fs.files[".ssh/authorized_keys"].decode().splitlines()
    assert content[0] == "ssh-rsa AAAAUSERKEYONE alpha@old"
    assert any("sgc-direct:srv-src:srv-other:zz9" in line for line in content)
    assert not any(f"sgc-direct:{SRC}:{DST}" in line for line in content)
    src_fs = manager.fs_for(servers[SRC])
    assert not any("sgc-direct-srv-src-srv-dst" in name for name in src_fs.files)
    assert service._metadata.get(SRC, DST) is None
    assert service._metadata.get(SRC, OTHER) is not None
    # The app-owned known_hosts may keep the entry (documented behavior).
    assert KNOWN_HOSTS_PATH in src_fs.files

    again = await service.revoke(SRC, DST)
    assert again.configured is False
    assert again.available is None
    assert again.reason is None


async def test_revoke_keeps_metadata_on_partial_failure(tmp_path: Path) -> None:
    servers = _servers()
    manager = FakeSshManager()
    service = _service(tmp_path, manager, servers)
    _verified_host_key_stub(service)
    manager.fs_for(servers[DST]).files[".ssh/authorized_keys"] = _USER_AUTH
    manager.queue_probe(0, "")
    assert (await service.setup_key(SRC, DST)).available is True

    manager.fs_for(servers[DST]).fail_reads = True
    result = await service.revoke(SRC, DST)

    assert result.configured is False  # NEVER claims configured on failure
    assert result.available is False
    assert "authorized_keys line could not be removed" in (result.reason or "")
    assert service._metadata.get(SRC, DST) is not None  # kept for a retry

    manager.fs_for(servers[DST]).fail_reads = False
    retry = await service.revoke(SRC, DST)
    assert retry.configured is False
    assert service._metadata.get(SRC, DST) is None


# ---- 6. zero-SSH listing + deletion bookkeeping -----------------------------------------------


def test_list_for_shape_and_forget_server(tmp_path: Path) -> None:
    servers = _servers()
    service = _service(tmp_path, FakeSshManager(), servers)

    assert (
        service.list_for(SRC) == SimpleNamespace(server_id=SRC, pairs=[])
        or service.list_for(SRC).pairs == []
    )
    assert service.list_for(SRC).server_id == SRC

    service._metadata.put(_stored_record(SRC, DST, "kid11"))
    service._metadata.put(_stored_record(OTHER, SRC, "kid12"))
    service._metadata.put(_stored_record(OTHER, DST, "kid13"))  # not involving SRC

    listing = service.list_for(SRC)
    assert listing.server_id == SRC
    assert {(p.source_server_id, p.target_server_id) for p in listing.pairs} == {
        (SRC, DST),
        (OTHER, SRC),
    }
    for pair in listing.pairs:
        assert pair.configured is True
        assert pair.method == "sgc_key"
        assert pair.available is None

    service._latest_checks[(SRC.casefold(), DST.casefold())] = SimpleNamespace(
        available=False, reason="authentication_failed", checked_at="2026-01-02T00:00:00+00:00"
    )
    direct = next(p for p in service.list_for(SRC).pairs if p.target_server_id == DST)
    assert direct.available is False
    assert direct.reason == "authentication_failed"
    assert direct.checked_at == "2026-01-02T00:00:00+00:00"

    assert service.forget_server(SRC) == 2
    assert service.list_for(SRC).pairs == []
    assert len(service._metadata.for_server(DST)) == 1  # OTHER<->DST row survives


def test_dedicated_key_options_projection(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeSshManager(), _servers())
    assert service.dedicated_key_options(SRC, DST) is None
    service._metadata.put(_stored_record(SRC, DST, "kid11"))
    assert service.dedicated_key_options(SRC, DST) == _DEDICATED


async def test_check_and_setup_reject_unknown_or_disabled(tmp_path: Path) -> None:
    servers = _servers()
    service = _service(tmp_path, FakeSshManager(), servers)
    with pytest.raises(NotFoundError):
        await service.check("srv-ghost", DST)
    with pytest.raises(NotFoundError):
        await service.setup_key(SRC, "srv-ghost")
    servers[DST].enabled = False
    with pytest.raises(ConflictError):
        await service.check(SRC, DST)
    with pytest.raises(ConflictError):
        await service.setup_key(SRC, DST)
    with pytest.raises(ConflictError):
        await service.setup_key(SRC, SRC)


# ---- 7. dedicated probe + rsync command strings (spec item 26) ---------------------------------


async def test_dedicated_probe_command_string_exact() -> None:
    manager = FakeSshManager()
    manager.queue_probe(0, "")
    session = await manager.transfer_command_session(SimpleNamespace(server_id="s"))
    options = DedicatedKeyOptions(
        private_key_path="/home/u/.ssh/gpu-console/keys/pair",
        known_hosts_path="/home/u/.ssh/gpu-console/known_hosts",
    )
    ok, detail = await dedicated_batch_mode_probe(
        session, TARGET_HOST, TARGET_PORT, TARGET_USER, options
    )
    assert ok is True and detail == ""
    assert session.commands == [
        "ssh -i /home/u/.ssh/gpu-console/keys/pair"
        " -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
        " -o UserKnownHostsFile=/home/u/.ssh/gpu-console/known_hosts -o ConnectTimeout=8"
        f" -p {TARGET_PORT} {TARGET_USER}@{TARGET_HOST} true"
    ]


# ---- 8. planner integration (spec items 25-26) -------------------------------------------------


class _RsyncExecutor:
    def __init__(self, *, rsync_ok: bool = True) -> None:
        self.rsync_ok = rsync_ok

    async def run(self, command: str, *, timeout_s: float = 10.0) -> Any:
        if "command -v rsync" in command:
            if self.rsync_ok:
                return SimpleNamespace(
                    exit_code=0, stdout="/usr/bin/rsync\n", stderr="", duration_ms=1.0
                )
            return SimpleNamespace(exit_code=127, stdout="", stderr="not found", duration_ms=1.0)
        return SimpleNamespace(exit_code=0, stdout="", stderr="", duration_ms=1.0)


class _PlanProbeSession:
    def __init__(self, outcomes: list[tuple[int, str]]) -> None:
        self.commands: list[str] = []
        self._outcomes = list(outcomes)
        self.closed = False

    async def run(
        self, command: str, *, timeout_s: float, on_stdout: Any = None, on_stderr: Any = None
    ) -> int:
        self.commands.append(command)
        code, stderr = self._outcomes.pop(0) if self._outcomes else (255, "")
        if on_stderr is not None and stderr:
            await on_stderr(stderr)
        return code

    async def close(self) -> None:
        self.closed = True


class _PlanSsh:
    def __init__(self, session: _PlanProbeSession) -> None:
        self._session = session

    def resolve_params_for(self, server: Any) -> Any:
        return SimpleNamespace(
            host=server.ssh_host, port=server.port or 22, username=server.username
        )

    async def transfer_command_session(self, server: Any) -> _PlanProbeSession:
        del server
        return self._session


def _plan_servers() -> tuple[Any, Any]:
    return (
        SimpleNamespace(server_id=SRC, ssh_host="source-node.example", username=None, port=22),
        SimpleNamespace(
            server_id=DST, ssh_host=TARGET_HOST, username=TARGET_USER, port=TARGET_PORT
        ),
    )


async def _plan(
    strategy: TransferStrategy,
    outcomes: list[tuple[int, str]],
    *,
    dedicated: bool = False,
) -> Any:
    source_server, target_server = _plan_servers()
    return await plan_transfer(
        requested=strategy,
        source_server=source_server,
        target_server=target_server,
        source_executor=_RsyncExecutor(),
        target_executor=_RsyncExecutor(),
        source_path="/srv/data",
        target_path="/srv/data",
        artifact_id="artifact-1",
        ssh=_PlanSsh(_PlanProbeSession(outcomes)),  # type: ignore[arg-type]
        dedicated_key=_DEDICATED if dedicated else None,
    )


async def test_planner_native_selected() -> None:
    plan = await _plan(TransferStrategy.AUTO, [(0, "")])
    assert plan.strategy_selected == TransferStrategy.DIRECT_RSYNC
    assert plan.direct_auth_method == "native"
    assert plan.direct_auth_reason is None
    assert plan.reason == "direct rsync preflight passed (auto)"


async def test_planner_sgc_key_selected_when_dedicated_probe_passes() -> None:
    plan = await _plan(
        TransferStrategy.DIRECT_RSYNC,
        [(255, "Permission denied (publickey)."), (0, "")],
        dedicated=True,
    )
    assert plan.strategy_selected == TransferStrategy.DIRECT_RSYNC
    assert plan.direct_auth_method == "sgc_key"
    assert plan.reason == "direct rsync preflight passed (sgc_key)"


async def test_planner_unavailable_falls_back_to_relay() -> None:
    plan = await _plan(
        TransferStrategy.AUTO,
        [(255, "Permission denied (publickey)."), (255, "Permission denied (publickey).")],
        dedicated=True,
    )
    assert plan.strategy_selected == TransferStrategy.LOCAL_RELAY
    assert plan.direct_auth_method is None
    assert plan.direct_auth_reason is None
    assert "direct rsync unavailable" in plan.reason
    assert "Permission denied" in plan.reason


async def test_planner_rsync_missing_reports_reason_code() -> None:
    source_server, target_server = _plan_servers()
    plan = await plan_transfer(
        requested=TransferStrategy.AUTO,
        source_server=source_server,
        target_server=target_server,
        source_executor=_RsyncExecutor(rsync_ok=False),
        target_executor=_RsyncExecutor(),
        source_path="/srv/data",
        target_path="/srv/data",
        artifact_id="artifact-1",
        ssh=_PlanSsh(_PlanProbeSession([])),  # type: ignore[arg-type]
    )
    assert plan.strategy_selected == TransferStrategy.LOCAL_RELAY
    assert plan.direct_auth_reason == "rsync_missing_source"


def test_rsync_command_with_dedicated_key_exact() -> None:
    command = build_rsync_command(
        source_path="/srv/data",
        target_spec=shlex.quote(f"{TARGET_USER}@{TARGET_HOST}:/srv/data"),
        target_port=TARGET_PORT,
        dedicated_key=_DEDICATED,
    )
    tokens = shlex.split(command)
    ssh_opts = tokens[tokens.index("-e") + 1]
    assert ssh_opts == (
        "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes"
        f" -i {_DEDICATED.private_key_path}"
        " -o IdentitiesOnly=yes"
        f" -o UserKnownHostsFile={_DEDICATED.known_hosts_path}"
        f" -p {TARGET_PORT}"
    )
    assert "sshpass" not in command
    assert "SSH_ASKPASS" not in command
    assert "ForwardAgent" not in command


def test_rsync_command_without_dedicated_key_unchanged() -> None:
    command = build_rsync_command(source_path="/s", target_spec="u@h:/d", target_port=None)
    tokens = shlex.split(command)
    ssh_opts = tokens[tokens.index("-e") + 1]
    assert ssh_opts == "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes"


# ---- 9. API wiring (pinned shapes) ---------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from app.api.v1.servers import router
    from app.runtime import Runtime, set_runtime

    registry = ServerRegistry(JsonFileStore(tmp_path / "registry.json"))
    set_default_registry(registry)
    manager = FakeSshManager()
    service = DirectAuthService(
        Settings(data_dir=tmp_path),
        manager,  # type: ignore[arg-type]
        lambda server_id: registry.get(server_id),
        lambda server_id: _registry_has(registry, server_id),
    )
    monkeypatch.setattr(DirectAuthService, "_verified_host_key", _api_verified)
    set_runtime(
        Runtime(  # type: ignore[arg-type]
            settings=Settings(data_dir=tmp_path),
            ssh=manager,  # type: ignore[arg-type]
            telemetry=object(),  # type: ignore[arg-type]
            direct_auth=service,  # type: ignore[arg-type]
        )
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    source_id = client.post(
        "/api/v1/servers",
        json={"display_name": "Src Box", "ssh_host": "source-node.example", "port": 2222},
    ).json()["server_id"]
    target_id = client.post(
        "/api/v1/servers",
        json={"display_name": "Dst Box", "ssh_host": TARGET_HOST, "port": TARGET_PORT},
    ).json()["server_id"]
    try:
        yield client, service, manager, source_id, target_id
    finally:
        set_runtime(None)
        set_default_registry(None)


async def _api_verified(self: object, server: object) -> VerifiedHostKey:
    del self, server
    return VerifiedHostKey(
        key_type="ssh-ed25519", openssh_public_key_line=_PUB_LINE, fingerprint="SHA256:verified"
    )


def _registry_has(registry: ServerRegistry, server_id: str) -> bool:
    try:
        registry.get(server_id)
    except NotFoundError:
        return False
    return True


def test_api_direct_auth_shapes(api: Any) -> None:
    client, _service, manager, source_id, target_id = api

    assert client.get(f"/api/v1/servers/{source_id}/direct-auth").json() == {
        "server_id": source_id,
        "pairs": [],
    }
    assert client.get("/api/v1/servers/ghost/direct-auth").status_code == 404

    manager.queue_probe(0, "")
    setup = client.post(f"/api/v1/servers/{source_id}/direct-auth/{target_id}/setup-key")
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["configured"] is True
    assert body["method"] == "sgc_key"
    assert body["available"] is True

    listing = client.get(f"/api/v1/servers/{source_id}/direct-auth").json()
    assert listing["server_id"] == source_id
    assert len(listing["pairs"]) == 1
    pair = listing["pairs"][0]
    assert pair["source_server_id"] == source_id
    assert pair["target_server_id"] == target_id
    assert pair["configured"] is True
    assert pair["method"] == "sgc_key"
    assert pair["available"] is True  # projected latest RAM check from the setup
    assert pair["reason"] is None
    assert pair["checked_at"] is not None

    deleted = client.delete(f"/api/v1/servers/{source_id}/direct-auth/{target_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"configured": False}
    assert client.get(f"/api/v1/servers/{source_id}/direct-auth").json() == {
        "server_id": source_id,
        "pairs": [],
    }


def test_api_check_endpoint(api: Any) -> None:
    client, _service, manager, source_id, target_id = api
    manager.queue_probe(0, "")
    ok = client.post(f"/api/v1/servers/{source_id}/direct-auth/{target_id}/check")
    assert ok.status_code == 200, ok.text
    assert ok.json()["available"] is True
    assert ok.json()["method"] == "native"
    missing = client.post(f"/api/v1/servers/ghost/direct-auth/{target_id}/check")
    assert missing.status_code == 404


def test_api_server_deletion_forgets_pair_rows(api: Any) -> None:
    client, _service, manager, source_id, target_id = api
    manager.queue_probe(0, "")
    assert (
        client.post(f"/api/v1/servers/{source_id}/direct-auth/{target_id}/setup-key").status_code
        == 200
    )
    assert client.delete(f"/api/v1/servers/{source_id}").status_code == 204
    listing = client.get(f"/api/v1/servers/{target_id}/direct-auth")
    assert listing.status_code == 200
    assert listing.json() == {"server_id": target_id, "pairs": []}


# ---- 10. transport-level verified host key (offline; real asyncssh SSHKey object) ---------------


async def test_verified_server_host_key_exports_pure_data() -> None:
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    line = key.export_public_key(format_name="openssh").decode("ascii").strip()

    class _Underlying:
        def get_server_host_key(self) -> object:
            return key

    class _FakeConn:
        conn = _Underlying()

    class _Manager:
        async def acquire_connection(self, server: object) -> _FakeConn:
            del server
            return _FakeConn()

    server = _record("srv-x", host="node.example", port=2207, username="u")
    verified = await verified_server_host_key(_Manager(), server)  # type: ignore[arg-type]
    assert verified is not None
    assert verified.key_type == "ssh-ed25519"
    assert verified.openssh_public_key_line == line
    assert verified.fingerprint.startswith("SHA256:")
    assert verified.known_hosts_entry("node.example", 2207) == f"[node.example]:2207 {line}"
    assert verified.known_hosts_entry("node.example", 22) == f"node.example {line}"


async def test_verified_server_host_key_none_cases() -> None:
    class _Keyless:
        def get_server_host_key(self) -> object:
            return None

    class _ManagerNoConn:
        async def acquire_connection(self, server: object) -> object:
            del server
            return SimpleNamespace(conn=None)

    class _ManagerNoKey:
        async def acquire_connection(self, server: object) -> object:
            del server
            return SimpleNamespace(conn=_Keyless())

    server = _record("srv-y", host="h.example", port=22, username="u")
    assert await verified_server_host_key(_ManagerNoConn(), server) is None  # type: ignore[arg-type]
    assert await verified_server_host_key(_ManagerNoKey(), server) is None  # type: ignore[arg-type]


async def test_sftp_transfer_session_set_mode_uses_chmod() -> None:
    calls: list[tuple[str, int]] = []

    class _FakeSftpClient:
        async def chmod(self, path: str, mode: int) -> None:
            calls.append((path, mode))

    connection = SimpleNamespace(is_closed=lambda: False, close=lambda: None)
    session = SftpTransferSession(connection, _FakeSftpClient(), Settings())
    await session.set_mode("/remote/path", 0o600)
    assert calls == [("/remote/path", 0o600)]


# ---- 11. repo scan: forbidden strings appear NOWHERE in app/ ------------------------------------


def test_forbidden_strings_absent_from_app_tree() -> None:
    app_root = Path(__file__).resolve().parents[1] / "app"
    forbidden = (
        "StrictHostKeyChecking=no",
        "UserKnownHostsFile=/dev/null",
        "sshpass",
        "ForwardAgent=yes",
    )
    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(app_root)}: {needle}")
    assert offenders == []

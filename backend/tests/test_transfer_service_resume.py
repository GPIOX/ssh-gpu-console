"""Phase 3 TransferService integration tests: immutable propagation, cancel
traversal into the running rsync, service-instance-independent resume over
deterministic partials, the disk-space preflight gate, strategy_reason
propagation, executor availability during a transfer, and the target-lock 409
passthrough.

Self-contained harness (no transport modules, no import of tests.test_transfer):
the relay is monkeypatched AT the strategy boundary with the pinned new
signature, so these tests never depend on local_relay internals.
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.models.transfer import TransferRequest, TransferState, TransferStrategy
from app.models.workspace import ArtifactCreate, PlacementCreate
from app.ssh.executor import RemoteCommandResult
from app.ssh.file_transfer import FileStat, ServerLike
from app.transfer.service import TransferService
from app.transfer.strategies import local_relay
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService

# ---- in-memory session fakes ------------------------------------------------------


class MemFS:
    """In-memory tree: dirs as a set, files as path->bytes."""

    def __init__(self) -> None:
        self.dirs: set[str] = {"/"}
        self.files: dict[str, bytes] = {}

    def stat(self, path: str) -> FileStat:
        if path in self.files:
            return FileStat(exists=True, is_dir=False, size_b=len(self.files[path]))
        if path in self.dirs or path == "/" or path == "~":
            return FileStat(exists=True, is_dir=True)
        return FileStat(exists=False, is_dir=False)

    def add_file(self, path: str, content: bytes) -> None:
        self.files[path] = content

    def add_dir(self, path: str) -> None:
        self.dirs.add(path)


class FakeTransferSession:
    """TransferSession over an in-memory FS (stat/listdir/read/write only)."""

    def __init__(self, fs: MemFS) -> None:
        self._fs = fs
        self.closed = False

    async def stat(self, path: str) -> FileStat:
        return self._fs.stat(path)

    async def mkdir(self, path: str) -> None:
        self._fs.add_dir(path)

    async def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return [
            entry[len(prefix) :]
            for entry in (*self._fs.files, *self._fs.dirs)
            if entry.startswith(prefix) and "/" not in entry[len(prefix) :]
        ]

    async def open_reader(self, path: str) -> Any:
        return _Reader(self._fs.files.get(path, b""))

    async def open_writer(self, path: str) -> Any:
        return _Writer(path, self._fs)

    async def close(self) -> None:
        self.closed = True


class _Reader:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._pos = 0

    async def read(self, size: int) -> bytes:
        chunk = self._content[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


class _Writer:
    def __init__(self, path: str, fs: MemFS) -> None:
        self._path = path
        self._fs = fs
        self._buffer = b""

    async def write(self, data: bytes) -> None:
        self._buffer += data

    async def close(self) -> None:
        self._fs.files[self._path] = self._buffer


class FakeLongSession:
    """Duck LongCommandSession: preflight probes succeed instantly; the rsync
    command blocks until cancel()/close() releases it — a stand-in for a
    remote rsync that would otherwise run to completion."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.cancel_called = False
        self.closed = False
        self.run_finished = False
        self._release = asyncio.Event()

    async def run(self, command: str, *, timeout_s: float, on_stdout: Any = None) -> int:
        self.commands.append(command)
        if command.startswith("ssh "):  # batch-mode source->target probe
            return 0
        try:
            await self._release.wait()
            return 0
        finally:
            self.run_finished = True

    async def cancel(self) -> None:
        # channel close: the remote sshd kills rsync right away
        self.cancel_called = True
        self._release.set()

    async def close(self) -> None:
        self.closed = True


class _Params:
    def __init__(self, host: str, port: int | None, username: str | None) -> None:
        self.host = host
        self.port = port
        self.username = username


class FakeSsh:
    """Duck SshManager: scripted executor results + fake sessions/slots.

    Executor commands are dispatched by substring exactly as the production
    planner/probes issue them; every scripted answer is per-test tunable.
    """

    def __init__(self, fs_by_server: dict[str, MemFS]) -> None:
        self._fs = fs_by_server
        self.long_session = FakeLongSession()
        self._slots: dict[str, asyncio.Semaphore] = {}
        self.rsync_present_exit = 0
        self.df_stdout = "Avail\n1073741824\n"  # 1 GiB free
        self.size_stdout = "1048576\n"  # 1 MiB source
        self.test_e_exit = 1  # target absent

    def transfer_slot_or_create(self, server_id: str) -> asyncio.Semaphore:
        slot = self._slots.get(server_id)
        if slot is None:
            slot = asyncio.Semaphore(1)
            self._slots[server_id] = slot
        return slot

    async def transfer_session(self, server: ServerLike) -> FakeTransferSession:
        return FakeTransferSession(self._fs[str(server.server_id)])

    async def transfer_command_session(self, server: ServerLike) -> FakeLongSession:
        return self.long_session

    async def close_transfer_sessions(self) -> None:
        return None

    async def close_all(self) -> None:
        return None

    def resolve_params_for(self, server: ServerLike) -> _Params:
        return _Params(
            host=str(server.ssh_host),
            port=getattr(server, "port", None) or 22,
            username=getattr(server, "username", None),
        )

    async def run(
        self, server: ServerLike, command: str, *, timeout_s: float = 10.0
    ) -> RemoteCommandResult:
        if "command -v rsync" in command:
            return RemoteCommandResult(self.rsync_present_exit, "/usr/bin/rsync\n", "", 1.0)
        if "df -B1" in command:
            return RemoteCommandResult(0, self.df_stdout, "", 1.0)
        if "du -sb" in command or "stat -c %s" in command:
            return RemoteCommandResult(0, self.size_stdout, "", 1.0)
        if "test -e" in command:
            return RemoteCommandResult(self.test_e_exit, "", "", 1.0)
        return RemoteCommandResult(0, "", "", 1.0)


# ---- harness ------------------------------------------------------------------------


class Harness:
    """Shared workspace/ssh/registry; new_service() builds a FRESH
    TransferService (fresh job registry) over the same infrastructure —
    exactly what a backend restart looks like to the transfer layer."""

    def __init__(self, tmp_path: Path) -> None:
        from app.models.server import ServerCreate
        from app.servers.registry import ServerRegistry, set_default_registry

        self.settings = Settings(
            data_dir=tmp_path / "data",
            max_transfers_global=2,
            transfer_chunk_size_b=65536,
            transfer_job_history=10,
        )
        self.workspace = WorkspaceService(
            WorkspaceRepository(_store(tmp_path / "workspace.json")),
            server_exists=lambda _sid: True,
        )
        registry = ServerRegistry(_store(tmp_path / "registry.json"))
        set_default_registry(registry)
        self.ids: dict[str, str] = {}
        self.fs_by_server: dict[str, MemFS] = {"srv-a": MemFS(), "srv-b": MemFS()}
        for name in list(self.fs_by_server):
            record = registry.create(ServerCreate(display_name=name, ssh_host=f"{name}.example"))
            self.ids[name] = record.server_id
            self.fs_by_server[record.server_id] = self.fs_by_server.pop(name)
        self.ssh = FakeSsh(self.fs_by_server)

    def new_service(self) -> TransferService:
        return TransferService(settings=self.settings, ssh=self.ssh, workspace=self.workspace)

    def artifact(self, kind: str, name: str, **kwargs: Any) -> Any:
        return self.workspace.create_artifact(ArtifactCreate(kind=kind, name=name, **kwargs))

    def placement(self, artifact_id: str, server_key: str, remote_path: str) -> Any:
        return self.workspace.create_placement(
            PlacementCreate(
                artifact_id=artifact_id,
                server_id=self.ids[server_key],
                remote_path=remote_path,
            )
        )


def _store(path: Path) -> Any:
    from app.persistence.json_store import JsonFileStore

    return JsonFileStore(path)


def _request(harness: Harness, artifact_id: str, placement_id: str, target: str) -> TransferRequest:
    return TransferRequest(
        artifact_id=artifact_id,
        source_placement_id=placement_id,
        target_server_id=harness.ids["srv-b"],
        target_path=target,
        strategy=TransferStrategy.LOCAL_RELAY,
    )


async def _wait_terminal(service: TransferService, job_id: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        job = service.job(job_id)
        last = job.state
        if job.state in (TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} stuck in {last}")


async def _wait_worker_done(service: TransferService, job_id: str, timeout: float = 5.0) -> None:
    """The worker popped its cancel event in its finally -> fully reaped."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job_id not in service._cancel_events:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"worker for {job_id} never finished")


# ---- relay fake (pinned NEW signature) ------------------------------------------------


def _partial_path(target_path: str) -> str:
    name = posixpath.basename(target_path)
    directory = posixpath.dirname(target_path) or "."
    return f"{directory}/.{name}.sgc-partial"


async def _copy_single_file(job: Any, source: Any, target: Any, chunk_size: int) -> None:
    reader = await source.open_reader(job.source_path)
    data = bytearray()
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            break
        data += chunk
    await reader.close()
    directory = posixpath.dirname(job.target_path) or "."
    await target.mkdir(directory)
    writer = await target.open_writer(job.target_path)
    await writer.write(bytes(data))
    await writer.close()


def _install_relay(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[dict[str, Any]],
    *,
    fail_first: bool = False,
    gate: asyncio.Event | None = None,
    entered: asyncio.Event | None = None,
) -> None:
    """Monkeypatch local_relay.relay_transfer with the pinned new signature.

    Behavior: records every invocation; optionally blocks mid-transfer
    (gate/entered); the first invocation can leave the deterministic partial
    behind and crash (resume setup); later invocations adopt the leftover
    partial into job.resumed_bytes and complete the copy.
    """
    count = {"n": 0}

    async def relay_transfer(
        *,
        job: Any,
        source: Any,
        target: Any,
        chunk_size: int,
        immutable: bool = False,
        excludes: tuple[str, ...] | list[str] = (),
        progress_interval_s: float = 0.5,
    ) -> None:
        count["n"] += 1
        calls.append(
            {
                "n": count["n"],
                "job_id": job.job_id,
                "job": job,
                "immutable": immutable,
                "excludes": list(excludes),
                "progress_interval_s": progress_interval_s,
            }
        )
        if entered is not None:
            entered.set()
        if gate is not None:
            await gate.wait()
        if fail_first and count["n"] == 1:
            directory = posixpath.dirname(job.target_path) or "."
            await target.mkdir(directory)
            writer = await target.open_writer(_partial_path(job.target_path))
            await writer.write(b"HALF-PAYLOAD")
            await writer.close()
            raise RuntimeError("simulated mid-transfer crash")
        existing = await target.stat(_partial_path(job.target_path))
        if existing.exists:
            job.resumed_bytes = existing.size_b  # resume semantics: partial reuse
        await _copy_single_file(job, source, target, chunk_size)

    monkeypatch.setattr(local_relay, "relay_transfer", relay_transfer)


# ---- 1. immutable propagation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sets_immutable_from_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    service = harness.new_service()
    calls: list[dict[str, Any]] = []
    _install_relay(monkeypatch, calls)

    dataset = harness.artifact("dataset", "IVMSD")
    code = harness.artifact("code", "tooling")
    explicit = harness.artifact("dataset", "MUTABLE-DSET", immutable=False)
    specs = [
        (dataset, "~/data/d1", "~/out/d1"),
        (code, "~/data/c1", "~/out/c1"),
        (explicit, "~/data/m1", "~/out/m1"),
    ]
    job_ids = []
    for artifact, source, target in specs:
        placement = harness.placement(artifact.artifact_id, "srv-a", source)
        request = _request(harness, artifact.artifact_id, placement.placement_id, target)
        job_ids.append(service.create(request)["job_id"])
    for job_id in job_ids:
        await _wait_terminal(service, job_id)
    assert service.job(job_ids[0]).immutable is True  # dataset default
    assert service.job(job_ids[1]).immutable is False  # code default
    assert service.job(job_ids[2]).immutable is False  # explicit False wins


# ---- 2. cancel reaches the running rsync ------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_kills_running_rsync_and_cleans_up(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.ssh.rsync_present_exit = 0
    service = harness.new_service()
    artifact = harness.artifact("dataset", "BIG")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data")

    created = service.create(
        TransferRequest(
            artifact_id=artifact.artifact_id,
            source_placement_id=placement.placement_id,
            target_server_id=harness.ids["srv-b"],
            target_path="~/out",
            strategy=TransferStrategy.DIRECT_RSYNC,
        )
    )
    job_id = created["job_id"]
    session = harness.ssh.long_session
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(session.commands) < 2:
        await asyncio.sleep(0.02)  # probe + rsync command issued
    assert len(session.commands) >= 2, "rsync command never reached the fake session"
    await asyncio.sleep(0.05)  # let run() park inside the (blocking) remote command

    service.cancel(job_id)
    await _wait_worker_done(service, job_id)

    assert session.cancel_called  # channel closed -> remote rsync terminates
    assert session.closed
    assert session.run_finished  # runner task reaped, nothing dangling
    job = service.job(job_id)
    assert job.state == TransferState.CANCELLED
    assert job_id not in service._cancel_events
    assert [entry.job_id for entry in service._jobs.history()].count(job_id) == 1


# ---- 3. restart-independent resume over the deterministic partial -----------------------


@pytest.mark.asyncio
async def test_resume_adopts_partial_after_service_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_relay(monkeypatch, calls, fail_first=True)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/blob.bin", b"A" * 3000)
    artifact = harness.artifact("dataset", "BLOB")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/blob.bin")
    request = _request(harness, artifact.artifact_id, placement.placement_id, "~/mirror/blob.bin")

    service_a = harness.new_service()
    first = service_a.create(request)
    await _wait_terminal(service_a, first["job_id"])
    assert service_a.job(first["job_id"]).state == TransferState.FAILED
    target_fs = harness.fs_by_server[harness.ids["srv-b"]]
    partial = target_fs.files.get(_partial_path("~/mirror/blob.bin"))
    assert partial == b"HALF-PAYLOAD"  # partial survived the crash on the target

    # "restart": a fresh service instance knows nothing about the old job object
    service_b = harness.new_service()
    with pytest.raises(NotFoundError):
        service_b.job(first["job_id"])
    resumed = service_b.create(request)
    await _wait_terminal(service_b, resumed["job_id"])
    job_b = service_b.job(resumed["job_id"])
    assert job_b.state == TransferState.COMPLETED
    assert calls[1]["job_id"] != calls[0]["job_id"]
    assert calls[1]["immutable"] is True
    assert job_b.resumed_bytes == len(b"HALF-PAYLOAD")  # partial adopted on the new job
    assert harness.fs_by_server[harness.ids["srv-b"]].files["~/mirror/blob.bin"] == b"A" * 3000

    # retry() also just re-queues parameters as a NEW job over the same partial
    retried = await service_a.retry(first["job_id"])
    assert retried["job_id"] != first["job_id"]
    await _wait_terminal(service_a, retried["job_id"])
    assert service_a.job(retried["job_id"]).resumed_bytes == len(b"HALF-PAYLOAD")
    assert len(calls) == 3


# ---- 4. disk-space preflight gate --------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_space_and_missing_target_fails_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    harness.ssh.size_stdout = "10485760\n"  # 10 MiB needed
    harness.ssh.df_stdout = "Avail\n4096\n"  # 4 KiB free
    harness.ssh.test_e_exit = 1  # target does not exist
    calls: list[dict[str, Any]] = []
    _install_relay(monkeypatch, calls)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/d.bin", b"x" * 2048)
    artifact = harness.artifact("dataset", "DSET")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/d.bin")

    service = harness.new_service()
    created = service.create(
        _request(harness, artifact.artifact_id, placement.placement_id, "~/out/d.bin")
    )
    job = await _wait_terminal(service, created["job_id"])
    assert job.state == TransferState.FAILED
    assert job.error_code == "insufficient_space"
    assert "10485760" in job.error_message and "4096" in job.error_message
    assert calls == []  # never started transferring


@pytest.mark.asyncio
async def test_insufficient_space_with_existing_target_only_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    harness.ssh.size_stdout = "10485760\n"
    harness.ssh.df_stdout = "Avail\n4096\n"
    harness.ssh.test_e_exit = 0  # target already exists -> incremental data
    calls: list[dict[str, Any]] = []
    _install_relay(monkeypatch, calls)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/d.bin", b"x" * 2048)
    artifact = harness.artifact("dataset", "DSET")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/d.bin")

    service = harness.new_service()
    created = service.create(
        _request(harness, artifact.artifact_id, placement.placement_id, "~/out/d.bin")
    )
    job = await _wait_terminal(service, created["job_id"])
    assert job.error_code != "insufficient_space"  # not failed during planning
    assert job.warnings, "existing target must produce a warning, not a refusal"
    assert "4096" in job.warnings[0] and "10485760" in job.warnings[0]


# ---- 5. strategy_reason propagation ------------------------------------------------------


@pytest.mark.asyncio
async def test_strategy_reason_propagates_from_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    calls: list[dict[str, Any]] = []
    _install_relay(monkeypatch, calls)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/c.py", b"print(1)")
    artifact = harness.artifact("code", "TOOL")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/c.py")

    service = harness.new_service()
    created = service.create(
        _request(harness, artifact.artifact_id, placement.placement_id, "~/out/c.py")
    )
    job = await _wait_terminal(service, created["job_id"])
    assert job.strategy_reason == "local relay requested"
    assert job.strategy_used == TransferStrategy.LOCAL_RELAY


# ---- 6. executor stays usable while a transfer runs ---------------------------------------


@pytest.mark.asyncio
async def test_executor_usable_during_active_transfer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    calls: list[dict[str, Any]] = []
    gate = asyncio.Event()
    entered = asyncio.Event()
    _install_relay(monkeypatch, calls, gate=gate, entered=entered)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/m.bin", b"m" * 1024)
    artifact = harness.artifact("dataset", "M")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/m.bin")

    service = harness.new_service()
    created = service.create(
        _request(harness, artifact.artifact_id, placement.placement_id, "~/out/m.bin")
    )
    job_id = created["job_id"]
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)  # mid-transfer now
        from app.servers.registry import get_default_registry

        record = get_default_registry().get(harness.ids["srv-a"])
        result = await harness.ssh.run(record, "command -v rsync", timeout_s=10)
        assert result.exit_code == 0 and "rsync" in result.stdout
    finally:
        gate.set()
    job = await _wait_terminal(service, job_id)
    assert job.state == TransferState.COMPLETED


# ---- 7. same-target concurrency surfaces as ConflictError ---------------------------------


@pytest.mark.asyncio
async def test_same_target_second_create_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    calls: list[dict[str, Any]] = []
    gate = asyncio.Event()
    entered = asyncio.Event()
    _install_relay(monkeypatch, calls, gate=gate, entered=entered)
    harness.fs_by_server[harness.ids["srv-a"]].add_file("~/data/a.bin", b"a" * 512)
    artifact = harness.artifact("dataset", "A")
    placement = harness.placement(artifact.artifact_id, "srv-a", "~/data/a.bin")

    service = harness.new_service()
    first = service.create(_request(harness, artifact.artifact_id, placement.placement_id, "~/out"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)  # lock held while running
        with pytest.raises(ConflictError, match="already has an active transfer"):
            service.create(_request(harness, artifact.artifact_id, placement.placement_id, "~/out"))
    finally:
        gate.set()
    await _wait_terminal(service, first["job_id"])

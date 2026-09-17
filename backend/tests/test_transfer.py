"""Phase 2 transfer tests: planner, local relay, rsync safety, concurrency.

Fakes are in-memory only (no network, no real SFTP); the local relay is
exercised against a scripted in-RAM filesystem to prove the no-staging-file
and bounded-chunk guarantees.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.models.transfer import (
    TransferJob,
    TransferRequest,
    TransferState,
    TransferStrategy,
)
from app.models.workspace import ArtifactCreate, PlacementCreate
from app.ssh.executor import RemoteCommandResult
from app.ssh.file_transfer import FileStat, ServerLike
from app.transfer.planner import (
    build_rsync_command,
    target_rsync_spec,
)
from app.transfer.service import TransferService
from app.transfer.state import JobRegistry
from app.transfer.strategies.local_relay import partial_name, relay_transfer
from app.transfer.verifier import quick_verify
from app.workspace.repository import WorkspaceRepository
from app.workspace.service import WorkspaceService

# ---- in-memory TransferSession fake -------------------------------------------


class MemFS:
    """In-memory tree: dirs as a set, files as path->bytes (mtime-aware so
    the relay's incremental checks stay honest against this fake)."""

    def __init__(self) -> None:
        self.dirs: set[str] = {"/"}
        self.files: dict[str, bytes] = {}
        self.mtimes: dict[str, int] = {}
        self.writes = 0

    def stat(self, path: str) -> FileStat:
        if path in self.files:
            return FileStat(
                exists=True,
                is_dir=False,
                size_b=len(self.files[path]),
                mtime_s=self.mtimes.get(path, 0),
            )
        if path in self.dirs or path == "/" or path == "~":
            return FileStat(exists=True, is_dir=True)
        return FileStat(exists=False, is_dir=False)

    def add_file(self, path: str, content: bytes) -> None:
        self.files[path] = content

    def add_dir(self, path: str) -> None:
        self.dirs.add(path)


class FakeTransferSession:
    """TransferSession over an in-memory FS; counts chunk reads."""

    def __init__(self, fs: MemFS, chunk_reads: list[int] | None = None) -> None:
        self._fs = fs
        self._chunk_reads = chunk_reads
        self.closed = False

    async def stat(self, path: str) -> FileStat:
        return self._fs.stat(path)

    async def lstat(self, path: str) -> FileStat:
        return self._fs.stat(path)

    async def mkdir(self, path: str) -> None:
        self._fs.add_dir(path)
        # parents implicit in this fake

    async def listdir(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        names = []
        for file_path in list(self._fs.files) + list(self._fs.dirs):
            if file_path.startswith(prefix) and "/" not in file_path[len(prefix) :]:
                names.append(file_path[len(prefix) :])
        return names

    async def open_reader(self, path: str, *, offset: int = 0) -> Any:
        content = self._fs.files.get(path, b"")
        return _FakeReader(content, self._chunk_reads, offset)

    async def open_writer(self, path: str, *, offset: int = 0, truncate: bool = False) -> Any:
        base = self._fs.files.get(path, b"")[:offset] if offset > 0 else b""
        return _FakeWriter(path, self._fs, self._chunk_reads, base)

    async def set_mtime(self, path: str, mtime_s: int) -> None:
        self._fs.mtimes[path] = mtime_s

    async def rename(self, source: str, target: str) -> None:
        if source in self._fs.files:
            self._fs.files[target] = self._fs.files.pop(source)
            if source in self._fs.mtimes:
                self._fs.mtimes[target] = self._fs.mtimes.pop(source)

    async def remove(self, path: str) -> None:
        self._fs.files.pop(path, None)
        self._fs.mtimes.pop(path, None)

    async def close(self) -> None:
        self.closed = True


class _FakeReader:
    def __init__(self, content: bytes, chunk_reads: list[int] | None, offset: int = 0) -> None:
        self._content = content
        self._pos = offset
        self._chunk_reads = chunk_reads

    async def read(self, size: int) -> bytes:
        if self._chunk_reads is not None:
            self._chunk_reads.append(size)
        chunk = self._content[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        return None


class _FakeWriter:
    def __init__(
        self,
        path: str,
        fs: MemFS,
        chunk_reads: list[int] | None,
        base: bytes = b"",
    ) -> None:
        self._path = path
        self._fs = fs
        self._buffer = bytearray(base)
        self._chunk_reads = chunk_reads

    async def write(self, data: bytes) -> None:
        if self._chunk_reads is not None:
            pass
        self._buffer += data

    async def close(self) -> None:
        self._fs.files[self._path] = bytes(self._buffer)


class _CancelledSession(FakeTransferSession):
    """Reader that raises once the job is cancelled (simulates remote loss)."""

    def __init__(self, fs: MemFS, job: Any, big: int) -> None:
        super().__init__(fs)
        self._job = job
        self._big = big

    async def open_reader(self, path: str) -> Any:
        return _CancellingReader(self._job, self._big)


class _CancellingReader:
    def __init__(self, job: Any, total: int) -> None:
        self._job = job
        self._pos = 0
        self._total = total

    async def read(self, size: int) -> bytes:
        if self._job.state.value == "cancelled":
            raise ConnectionError("source session lost after cancel")
        chunk = b"x" * size
        self._pos += size
        if self._pos >= self._total:
            return b""
        return chunk

    async def close(self) -> None:
        return None


# ---- fakes for the service layer -------------------------------------------------


class FakeSsh:
    """Duck SshManager for TransferService tests (no network)."""

    def __init__(self, settings: Settings, fs_by_server: dict[str, MemFS]) -> None:
        self._settings = settings
        self._fs = fs_by_server
        self._slots: dict[str, Any] = {}

    def transfer_slot_or_create(self, server_id: str) -> Any:
        import asyncio

        slot = self._slots.get(server_id)
        if slot is None:
            slot = asyncio.Semaphore(1)
            self._slots[server_id] = slot
        return slot

    async def transfer_session(self, server: ServerLike) -> FakeTransferSession:
        return FakeTransferSession(self._fs[server.server_id])

    async def transfer_command_session(self, server: ServerLike) -> Any:
        return _FakeLongCommand([], exit_code=0)

    async def close_transfer_sessions(self) -> None:
        return None

    async def close_all(self) -> None:
        return None

    def build_executor_for(self, server_id: str) -> Any:
        class _Exec:
            async def run(self, command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
                if "command -v rsync" in command:
                    return RemoteCommandResult(0, "/usr/bin/rsync\n", "", 1.0)
                if "stat -c %s" in command:
                    return RemoteCommandResult(0, "1048576\n", "", 1.0)
                return RemoteCommandResult(0, "", "", 1.0)

        return _Exec()

    def resolve_params_for(self, server: ServerLike) -> Any:

        return _ParamsStub(host=f"{server.ssh_host}.example", port=22, username=server.username)


class _ParamsStub:
    def __init__(self, host: str, port: int | None, username: str | None) -> None:
        self.host = host
        self.port = port
        self.username = username


class _FakeLongCommand:
    def __init__(self, calls: list[str], exit_code: int) -> None:
        self._calls = calls
        self._exit = exit_code
        self.closed = False

    async def run(self, command: str, *, timeout_s: float, on_stdout: Any = None) -> int:
        self._calls.append(command)
        return self._exit

    async def close(self) -> None:
        self.closed = True


# ---- service harness -------------------------------------------------------------


def _service(
    tmp_path: Path, fs_by_server: dict[str, MemFS]
) -> tuple[TransferService, WorkspaceService]:
    from app.servers.registry import ServerRegistry, set_default_registry

    settings = Settings(
        data_dir=tmp_path / "data",
        max_transfers_global=2,
        max_transfers_per_server=1,
        transfer_chunk_size_b=65536,
        transfer_job_history=10,
    )
    repo = WorkspaceRepository(_store(tmp_path / "workspace.json"))
    workspace = WorkspaceService(repo, server_exists=lambda _sid: True)
    registry = ServerRegistry(_store(tmp_path / "registry.json"))
    set_default_registry(registry)
    ids: dict[str, str] = {}
    for name in list(fs_by_server):
        from app.models.server import ServerCreate

        record = registry.create(ServerCreate(display_name=name, ssh_host=f"{name}.example"))
        ids[name] = record.server_id
        fs_by_server[record.server_id] = fs_by_server.pop(name)
    ssh = FakeSsh(settings, fs_by_server)
    service = TransferService(settings=settings, ssh=ssh, workspace=workspace)
    return service, workspace, ids


def _store(path: Path) -> Any:
    from app.persistence.json_store import JsonFileStore

    return JsonFileStore(path)


async def _wait_state(
    service: TransferService, job_id: str, state: TransferState, timeout: float = 5.0
) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.job(job_id)
        if job.state == state:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {state}")


# ---- local relay tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_relay_single_file_roundtrip() -> None:
    src, dst = MemFS(), MemFS()
    src.add_file("~/data/model.bin", b"x" * 4096)
    job = _job(source_path="~/data/model.bin", target_path="~/models/model.bin")
    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=65536,
        immutable=True,
    )
    assert dst.files["~/models/model.bin"] == b"x" * 4096
    assert job.files_done == 1 and job.bytes_done == 4096


@pytest.mark.asyncio
async def test_relay_directory_copy_update_not_mirror() -> None:
    src, dst = MemFS(), MemFS()
    src.add_dir("~/data/dset")
    src.add_file("~/data/dset/a.bin", b"A" * 2048)
    src.add_file("~/data/dset/b.bin", b"B" * 1024)
    dst.add_file("~/dset/EXTRA.txt", b"keep me")  # mirror would delete this

    job = _job(source_path="~/data/dset", target_path="~/dset")
    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=65536,
        immutable=True,
    )
    assert dst.files["~/dset/a.bin"] == b"A" * 2048
    assert dst.files["~/dset/b.bin"] == b"B" * 1024
    assert dst.files["~/dset/EXTRA.txt"] == b"keep me"  # no --delete semantics
    assert job.files_total == 2 and job.files_done == 2


@pytest.mark.asyncio
async def test_relay_uses_bounded_chunks_never_whole_file() -> None:
    reads: list[int] = []
    src, dst = MemFS(), MemFS()
    big = b"z" * (256 * 1024)  # 256 KiB payload over 64 KiB chunks
    src.add_file("~/d.bin", big)
    job = _job()
    await relay_transfer(
        job=job,
        source=FakeTransferSession(src, reads),
        target=FakeTransferSession(dst),
        chunk_size=65536,
        immutable=False,
    )
    assert reads and max(reads) <= 65536
    assert dst.files["~/d.bin"] == big


@pytest.mark.asyncio
async def test_relay_partial_file_never_corrupts_target() -> None:
    src, dst = MemFS(), MemFS()
    src.add_file("~/f.bin", b"new-content" * 100)
    dst.add_file("~/f.bin", b"ORIGINAL-CONTENT")

    job = _job(source_path="~/f.bin", target_path="~/f.bin")
    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=65536,
        immutable=False,
    )
    assert dst.files["~/f.bin"] == src.files["~/f.bin"]


def test_partial_name_format() -> None:
    assert partial_name("model.bin") == ".model.bin.sgc-partial"


# ---- planner / rsync safety -------------------------------------------------------


def test_rsync_command_builder_quotes_and_locks_flags() -> None:
    import shlex

    command = build_rsync_command(
        source_path="/data/my data;/evil",
        target_spec=target_rsync_spec(host="10.0.0.8", username="demo", target_path="/target dir"),
        target_port=2222,
    )
    assert "rm" not in command.replace("--", "")
    assert "--partial" in command and "--partial-dir=.sgc-rsync-partial" in command
    assert "--delete" not in command
    assert "--append" not in command
    assert command.count("'") >= 2  # quoted paths + quoted -e ssh options
    assert "StrictHostKeyChecking=yes" in command  # never accept-new / no known_hosts writes
    # the port is encoded exactly once, inside the -e ssh options
    assert command.count("2222") == 1
    tokens = shlex.split(command)
    rsync_tokens = tokens[: tokens.index("-e")]
    for banned in ("-a", "-o", "-g", "--delete", "--append", "--append-verify"):
        assert banned not in rsync_tokens
    for required in ("-r", "-l", "-t", "-p", "--safe-links", "--info=progress2"):
        assert required in rsync_tokens


def test_no_arbitrary_user_flags() -> None:
    import shlex

    command = build_rsync_command(source_path="/a", target_spec="'u@h:/b'", target_port=None)
    tokens = shlex.split(command)
    assert "--info=progress2" in tokens
    rsync_tokens = tokens[: tokens.index("-e")]
    for banned in ("-a", "-o", "-g", "--delete", "--append", "--append-verify"):
        assert banned not in rsync_tokens
    for required in ("-r", "-l", "-t", "-p", "--safe-links", "--partial"):
        assert required in rsync_tokens
    # a user cannot inject extra flags through paths: they are quoted
    assert tokens[tokens.index("--") + 1] == "/a"
    assert tokens[-1] == "u@h:/b"


# ---- service level tests ----------------------------------------------------------


@pytest.mark.asyncio
async def test_service_relay_end_to_end_records_placement_once(tmp_path: Path) -> None:
    fs_by_server = {"srv-a": MemFS(), "srv-b": MemFS()}
    fs_by_server["srv-a"].add_dir("~/data")
    fs_by_server["srv-a"].add_file("~/data/d.bin", b"payload" * 1000)
    service, workspace, ids = _service(tmp_path, fs_by_server)
    srv_a, srv_b = ids["srv-a"], ids["srv-b"]
    artifact = workspace.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD"))
    placement = workspace.create_placement(
        PlacementCreate(artifact_id=artifact.artifact_id, server_id=srv_a, remote_path="~/data")
    )
    result = service.create(
        TransferRequest(
            artifact_id=artifact.artifact_id,
            source_placement_id=placement.placement_id,
            target_server_id=srv_b,
            target_path="~/models/IVMSD",
            strategy=TransferStrategy.LOCAL_RELAY,
        )
    )
    job_id = result["job_id"]
    await _wait_state(service, job_id, TransferState.COMPLETED)
    job = service.job(job_id)
    assert job.state == TransferState.COMPLETED
    # auto-placement created ONCE
    placements = workspace.placements()
    assert len(placements) == 2
    assert {p.server_id for p in placements} == {srv_a, srv_b}


@pytest.mark.asyncio
async def test_service_cancel_before_planning(tmp_path: Path) -> None:
    service, workspace, ids = _service(tmp_path, {"srv-a": MemFS(), "srv-b": MemFS()})
    artifact = workspace.create_artifact(ArtifactCreate(kind="code", name="C"))
    placement = workspace.create_placement(
        PlacementCreate(artifact_id=artifact.artifact_id, server_id=ids["srv-a"], remote_path="~/c")
    )
    created = service.create(
        TransferRequest(
            artifact_id=artifact.artifact_id,
            source_placement_id=placement.placement_id,
            target_server_id=ids["srv-b"],
            target_path="~/c2",
        )
    )
    service.cancel(created["job_id"])
    await _wait_state(service, created["job_id"], TransferState.CANCELLED)


@pytest.mark.asyncio
async def test_service_retry_requeues(tmp_path: Path) -> None:
    fs_by_server = {"srv-a": MemFS(), "srv-b": MemFS()}
    fs_by_server["srv-a"].add_file("~/c", b"hello transfer")
    service, workspace, ids = _service(tmp_path, fs_by_server)
    artifact = workspace.create_artifact(ArtifactCreate(kind="code", name="C"))
    placement = workspace.create_placement(
        PlacementCreate(artifact_id=artifact.artifact_id, server_id=ids["srv-a"], remote_path="~/c")
    )
    first = service.create(
        TransferRequest(
            artifact_id=artifact.artifact_id,
            source_placement_id=placement.placement_id,
            target_server_id=ids["srv-b"],
            target_path="~/c2",
        )
    )
    await _wait_state(service, first["job_id"], TransferState.COMPLETED)
    retried = await service.retry(first["job_id"])
    assert retried["job_id"] != first["job_id"]


# ---- helpers -----------------------------------------------------------------------


def _job(**overrides: Any) -> TransferJob:

    payload: dict[str, Any] = {
        "artifact_id": "a1",
        "artifact_label": "IVMSD:v1",
        "source_server_id": "srv-a",
        "source_path": "~/d.bin",
        "target_server_id": "srv-b",
        "target_path": "~/d.bin",
        "strategy_requested": TransferStrategy.LOCAL_RELAY,
    }
    payload.update(overrides)
    return JobRegistry(10).create(**payload)


# ---- API layer ----------------------------------------------------------------------


def test_create_transfer_api_schedules_worker_on_app_loop(tmp_path: Path) -> None:
    """Regression: the create endpoint ran as a sync def (threadpool) where no
    event loop exists, so service.create() raised RuntimeError -> 500 while the
    plan preview had already succeeded. With TestClient this must reach 202 and
    the worker must run to completion on the app loop."""
    import time

    from app.core.lifecycle import AppContext
    from app.main import create_app
    from app.servers.registry import get_default_registry
    from app.telemetry.service import TelemetryService
    from fastapi.testclient import TestClient

    class _DummyExecutor:
        async def run(self, _command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
            return RemoteCommandResult(0, "", "", 1.0)

        async def close(self) -> None:
            return None

    fs_by_server = {"srv-a": MemFS(), "srv-b": MemFS()}
    fs_by_server["srv-a"].add_dir("~/data")
    fs_by_server["srv-a"].add_file("~/data/d.bin", b"payload" * 1000)
    service, workspace, ids = _service(tmp_path, fs_by_server)
    artifact = workspace.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD"))
    placement = workspace.create_placement(
        PlacementCreate(
            artifact_id=artifact.artifact_id,
            server_id=ids["srv-a"],
            remote_path="~/data",
        )
    )
    context = AppContext(
        service._settings,
        ssh=service._ssh,  # type: ignore[arg-type]
        telemetry=TelemetryService(
            service._settings,
            get_default_registry(),
            lambda _sid, _record: _DummyExecutor(),  # type: ignore[arg-type,return-value]
        ),
        workspace=workspace,
        transfers=service,
    )
    client = TestClient(create_app(service._settings, context=context))
    with client:
        response = client.post(
            "/api/v1/transfers",
            json={
                "artifact_id": artifact.artifact_id,
                "source_placement_id": placement.placement_id,
                "target_server_id": ids["srv-b"],
                "target_path": "~/models/IVMSD",
                "strategy": "local_relay",
            },
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        deadline = time.monotonic() + 5.0
        state = None
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/transfers/{job_id}").json()
            state = payload["state"]
            if state in ("completed", "failed"):
                break
            time.sleep(0.02)
        assert state == "completed", payload


@pytest.mark.asyncio
async def test_relay_survives_dot_entries_from_listing() -> None:
    """Real SFTP readdir includes "." and ".."; a session that passes them
    through must not make the directory walk loop forever (field-observed:
    the job sat at 0 bytes until cancelled)."""
    src, dst = MemFS(), MemFS()
    src.add_dir("~/data")
    src.add_dir("~/data/sub")
    src.add_file("~/data/model.bin", b"abc" * 100)
    src.add_file("~/data/sub/piece.bin", b"xyz" * 100)
    job = _job(source_path="~/data", target_path="~/mirror/data")

    class DottedSession(FakeTransferSession):
        async def listdir(self, path: str) -> list[str]:
            names = await super().listdir(path)
            return [".", "..", *names]

    await relay_transfer(
        job=job,
        source=DottedSession(src),
        target=DottedSession(dst),
        chunk_size=1024,
        immutable=True,
    )
    assert dst.files["~/mirror/data/model.bin"] == b"abc" * 100
    assert dst.files["~/mirror/data/sub/piece.bin"] == b"xyz" * 100
    assert job.files_done == 2


@pytest.mark.asyncio
async def test_relay_honors_project_excludes() -> None:
    """Excluded entry names are skipped in the walk (never counted, never
    copied); the transfer root's own name is not affected."""
    src, dst = MemFS(), MemFS()
    src.add_dir("~/app")
    src.add_dir("~/app/dataset")
    src.add_dir("~/app/src")
    src.add_file("~/app/dataset/D1.bin", b"heavy" * 100)
    src.add_file("~/app/src/main.py", b"code")
    job = _job(source_path="~/app", target_path="~/app-mirror", artifact_label="app")

    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=1024,
        excludes=("dataset", "*.log"),
        immutable=False,
    )
    assert dst.files["~/app-mirror/src/main.py"] == b"code"
    assert "~/app-mirror/dataset/D1.bin" not in dst.files
    assert job.files_done == 1


@pytest.mark.asyncio
async def test_relay_single_file_transfer_ignores_excludes() -> None:
    src, dst = MemFS(), MemFS()
    src.add_file("~/weights/model.bin", b"w" * 512)
    job = _job(source_path="~/weights/model.bin", target_path="~/mirror/model.bin")

    await relay_transfer(
        job=job,
        source=FakeTransferSession(src),
        target=FakeTransferSession(dst),
        chunk_size=1024,
        excludes=("*.bin",),
        immutable=False,
    )
    assert dst.files["~/mirror/model.bin"] == b"w" * 512


# ---- quick verify -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_verify_tolerates_files_added_mid_transfer() -> None:
    """Field regression: a live tree changed between the relay's count walk
    (files_total=277) and its copy walk (files_done=278); quick_verify must
    re-walk and pass when every current source file exists on the target."""
    src, dst = MemFS(), MemFS()
    src.add_dir("~/app")
    src.add_file("~/app/a.py", b"a")
    src.add_file("~/app/b.py", b"b")
    for path in ("~/mirror", "~/mirror/app"):
        dst.add_dir(path)
    dst.add_file("~/mirror/app/a.py", b"a")
    dst.add_file("~/mirror/app/b.py", b"b")
    dst.add_file("~/mirror/app/c-late.py", b"c")  # appeared after the count walk

    job = _job(source_path="~/app", target_path="~/mirror/app", artifact_label="x")
    job.files_total = 2
    job.files_done = 3

    ok, reason = await quick_verify(
        job=job, source=FakeTransferSession(src), target=FakeTransferSession(dst)
    )
    assert ok, reason
    assert "re-verified" in reason


@pytest.mark.asyncio
async def test_quick_verify_detects_missing_file() -> None:
    """Counters agree but the tree changed underneath: a mirrored file is
    absent on the target, so the walk must catch it and name the path."""
    src, dst = MemFS(), MemFS()
    src.add_dir("~/app")
    src.add_file("~/app/a.py", b"a")
    src.add_file("~/app/b.py", b"b")
    dst.add_dir("~/mirror")
    dst.add_dir("~/mirror/app")
    dst.add_file("~/mirror/app/a.py", b"a")

    job = _job(source_path="~/app", target_path="~/mirror/app", artifact_label="x")
    job.files_total = 2
    job.files_done = 2

    ok, reason = await quick_verify(
        job=job, source=FakeTransferSession(src), target=FakeTransferSession(dst)
    )
    assert not ok
    assert "~/mirror/app/b.py" in reason


@pytest.mark.asyncio
async def test_quick_verify_excludes_are_not_required_on_target() -> None:
    """Excluded entries are never counted, never copied — the re-walk must
    skip them too, so an absent-on-target excluded subtree passes."""
    src, dst = MemFS(), MemFS()
    src.add_dir("~/app")
    src.add_dir("~/app/dataset")
    src.add_file("~/app/dataset/D1.bin", b"heavy" * 100)
    src.add_file("~/app/main.py", b"code")
    dst.add_dir("~/mirror")
    dst.add_dir("~/mirror/app")
    dst.add_file("~/mirror/app/main.py", b"code")

    job = _job(
        source_path="~/app",
        target_path="~/mirror/app",
        artifact_label="x",
    )
    job.excludes = ["dataset"]
    job.files_total = 1
    job.files_done = 1

    ok, reason = await quick_verify(
        job=job, source=FakeTransferSession(src), target=FakeTransferSession(dst)
    )
    assert ok, reason


# ---- clear history -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_clear_history_keeps_active_jobs() -> None:
    registry = JobRegistry(10)
    completed = registry.create(
        artifact_id="a",
        artifact_label="A",
        source_server_id="srv-a",
        source_path="~/a",
        target_server_id="srv-b",
        target_path="~/a",
        strategy_requested=TransferStrategy.LOCAL_RELAY,
    )
    failed = registry.create(
        artifact_id="b",
        artifact_label="B",
        source_server_id="srv-a",
        source_path="~/b",
        target_server_id="srv-b",
        target_path="~/b",
        strategy_requested=TransferStrategy.LOCAL_RELAY,
    )
    cancelled = registry.create(
        artifact_id="c",
        artifact_label="C",
        source_server_id="srv-a",
        source_path="~/c",
        target_server_id="srv-b",
        target_path="~/c",
        strategy_requested=TransferStrategy.LOCAL_RELAY,
    )
    running = registry.create(
        artifact_id="d",
        artifact_label="D",
        source_server_id="srv-a",
        source_path="~/d",
        target_server_id="srv-b",
        target_path="~/d",
        strategy_requested=TransferStrategy.LOCAL_RELAY,
    )
    registry.transition(completed, TransferState.COMPLETED)
    registry.transition(failed, TransferState.FAILED)
    registry.transition(cancelled, TransferState.CANCELLED)
    registry.transition(running, TransferState.RUNNING)

    assert registry.clear_history() == 3
    assert registry.find(completed.job_id) is None
    assert registry.find(failed.job_id) is None
    assert registry.find(cancelled.job_id) is None
    assert registry.find(running.job_id) is running
    assert [job.job_id for job in registry.history()] == []
    assert registry.clear_history() == 0


def test_clear_transfer_history_api(tmp_path: Path) -> None:
    import time

    from app.core.lifecycle import AppContext
    from app.main import create_app
    from app.servers.registry import get_default_registry
    from app.telemetry.service import TelemetryService
    from fastapi.testclient import TestClient

    class _DummyExecutor:
        async def run(self, _command: str, *, timeout_s: float = 10.0) -> RemoteCommandResult:
            return RemoteCommandResult(0, "", "", 1.0)

        async def close(self) -> None:
            return None

    fs_by_server = {"srv-a": MemFS(), "srv-b": MemFS()}
    fs_by_server["srv-a"].add_dir("~/data")
    fs_by_server["srv-a"].add_file("~/data/d.bin", b"payload" * 1000)
    service, workspace, ids = _service(tmp_path, fs_by_server)
    artifact = workspace.create_artifact(ArtifactCreate(kind="dataset", name="IVMSD"))
    placement = workspace.create_placement(
        PlacementCreate(
            artifact_id=artifact.artifact_id,
            server_id=ids["srv-a"],
            remote_path="~/data",
        )
    )
    context = AppContext(
        service._settings,
        ssh=service._ssh,  # type: ignore[arg-type]
        telemetry=TelemetryService(
            service._settings,
            get_default_registry(),
            lambda _sid, _record: _DummyExecutor(),  # type: ignore[arg-type,return-value]
        ),
        workspace=workspace,
        transfers=service,
    )
    client = TestClient(create_app(service._settings, context=context))
    with client:
        response = client.post(
            "/api/v1/transfers",
            json={
                "artifact_id": artifact.artifact_id,
                "source_placement_id": placement.placement_id,
                "target_server_id": ids["srv-b"],
                "target_path": "~/models/IVMSD",
                "strategy": "local_relay",
            },
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        deadline = time.monotonic() + 5.0
        state = None
        payload = None
        while time.monotonic() < deadline:
            payload = client.get(f"/api/v1/transfers/{job_id}").json()
            state = payload["state"]
            if state in ("completed", "failed"):
                break
            time.sleep(0.02)
        assert state == "completed", payload

        cleared = client.delete("/api/v1/transfers")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cleared"] >= 1

        listing = client.get("/api/v1/transfers").json()
        assert job_id not in {job["job_id"] for job in listing}

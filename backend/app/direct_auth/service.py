"""Direct-auth service: check / setup-key / revoke for server→server rsync.

Phase 4.2C (spec items 25-28). Priority ladder: EXISTING_NATIVE_AUTH (the
source can already BatchMode-ssh the target) > SGC_DEDICATED_KEY (explicit
user click, spec item 27) > UNAVAILABLE → Local Relay. A password is NEVER
used for server-to-server rsync.

Security invariants implemented here:

- host-key verification is NEVER disabled: every probe carries
  StrictHostKeyChecking=yes, and the target key installed into the app-owned
  known_hosts on the source is the key VERIFIED by a real console handshake
  (never ssh-keyscan, never accept-new, never /dev/null);
- the user's own ``~/.ssh/known_hosts`` (source or local) is never written;
- remote file edits happen through SFTP (stat → read → temp write → rename),
  never shell one-liners with user data; user lines in authorized_keys are
  preserved byte-for-byte;
- revoke deletes ONLY this pair's own materials: the pair's key files on the
  source and the authorized_keys line with the pair's exact comment AND
  fingerprint. Never rm -rf, never authorized_keys wholesale clearing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import posixpath
import re
import shlex
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.direct_auth.metadata import DirectAuthMetadata
from app.models.direct_auth import (
    DirectAuthList,
    DirectAuthMethod,
    DirectAuthPairMetadata,
    DirectAuthPairView,
    DirectAuthStatus,
)
from app.models.server import ServerRecord
from app.persistence.json_store import JsonFileStore
from app.ssh.executor import ExecutorError

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import Settings
    from app.models.transfer import DedicatedKeyOptions
    from app.ssh.file_transfer import TransferSession
    from app.ssh.manager import SshManager
    from app.ssh.transport import VerifiedHostKey

logger = get_logger("direct_auth.service")

_SOURCE_SSH_DIR = ".ssh"
_SOURCE_APP_DIR = ".ssh/gpu-console"
_SOURCE_KEYS_DIR = ".ssh/gpu-console/keys"
_SOURCE_KNOWN_HOSTS = f"{_SOURCE_APP_DIR}/known_hosts"
_TARGET_AUTH_KEYS = f"{_SOURCE_SSH_DIR}/authorized_keys"

_DIR_MODE = 0o700
_KEY_FILE_MODE = 0o600
_RESTRICTED_OPTIONS = "no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty"

# Dedicated-check failure reasons that justify REPAIR on the already-configured
# path. route_unreachable is excluded: when the route is down even diagnostics
# are unreliable, and destroying a possibly-working key cannot help.
_ROUTE_REASON = "route_unreachable"

_SERVER_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class _SetupFailure(Exception):
    """A setup step failed with a taxonomy reason; the attempt is rolled back."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    """Filename-safe, deterministic reduction of a server id (path safety)."""

    cleaned = "".join(
        char if (char.isascii() and char.isalnum()) or char in "._-" else "_" for char in value
    )
    return (cleaned[:40] or "id").strip("._") or "id"


def _comment(source_server_id: str, target_server_id: str, key_id: str) -> str:
    return f"sgc-direct:{_slug(source_server_id)}:{_slug(target_server_id)}:{key_id}"


def _openssh_fingerprint(blob_b64: str) -> str:
    """OpenSSH SHA256 fingerprint ("SHA256:...") computed locally from the blob."""

    digest = hashlib.sha256(base64.b64decode(blob_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


def _line_fields(line: bytes) -> list[str] | None:
    try:
        return line.strip().decode("ascii").split()
    except UnicodeDecodeError:
        return None


def _line_comment(line: bytes) -> str | None:
    fields = _line_fields(line)
    return fields[-1] if fields and len(fields) >= 3 else None


def _line_blob(line: bytes) -> str | None:
    fields = _line_fields(line)
    return fields[-2] if fields and len(fields) >= 3 else None


def _classify_probe_failure(detail: str, *, dedicated: bool) -> str:
    """Map collected ssh stderr to the DirectAuthReason taxonomy (spec order).

    Order matters: ssh prints "Load key <path>: No such file or directory"
    BEFORE the final "Permission denied" when the identity file is absent, so
    the dedicated-key test comes first for dedicated probes.
    """

    lowered = detail.lower()
    if dedicated and (
        ("load key" in lowered and "no such file" in lowered)
        or ("identity file" in lowered and "not accessible" in lowered)
    ):
        return "dedicated_key_missing"
    if "permission denied" in lowered:
        return "authentication_failed"
    if "host key verification failed" in lowered:
        return "host_key_mismatch"
    if "host key" in lowered:  # remaining host-key context = never-trusted key
        return "host_key_unknown"
    if (
        "connection refused" in lowered
        or "connection timed out" in lowered
        or "timed out" in lowered
        or "could not resolve" in lowered
        or "no route to host" in lowered
    ):
        return "route_unreachable"
    return "unknown"


def _executor_reason(exc: ExecutorError) -> str:
    """Map a manager-level transport failure to the reason taxonomy."""

    code = exc.code
    if code == "authentication_failed":
        return "authentication_failed"
    if code == "host_key_mismatch":
        return "host_key_mismatch"
    if code == "host_key_unknown":
        return "host_key_unknown"
    if code == "unknown":
        return "unknown"
    return _ROUTE_REASON


class DirectAuthService:
    """Owns the direct-auth transaction; metadata is written only on setup/revoke."""

    def __init__(
        self,
        settings: Settings,
        ssh: SshManager,
        registry_lookup: Callable[[str], ServerRecord],
        workspace_like_server_lookup: Callable[[str], bool],
        *,
        metadata: DirectAuthMetadata | None = None,
    ) -> None:
        self._settings = settings
        self._ssh = ssh
        self._registry_lookup = registry_lookup
        self._server_exists = workspace_like_server_lookup
        self._metadata = metadata or DirectAuthMetadata(
            JsonFileStore(settings.data_dir / "direct_auth.json")
        )
        # Latest check result per pair — RAM only, never persisted.
        self._latest_checks: dict[tuple[str, str], DirectAuthStatus] = {}
        # Serializes remote-state mutations (setup/revoke) for the whole service.
        self._operation_lock = asyncio.Lock()

    # ---- resolution helpers ------------------------------------------------------------

    def _server(self, server_id: str) -> ServerRecord:
        if not _SERVER_ID_RE.fullmatch(server_id):
            raise NotFoundError(f"server {server_id!r} not found")
        try:
            server = self._registry_lookup(server_id)
        except (ConflictError, NotFoundError):
            raise
        except Exception as error:
            raise NotFoundError(f"server {server_id!r} not found") from error
        if server is None:
            raise NotFoundError(f"server {server_id!r} not found")
        return server

    @staticmethod
    def _require_enabled(servers: list[ServerRecord]) -> None:
        for server in servers:
            if not server.enabled:
                raise ConflictError(f"server {server.server_id!r} is disabled")

    @staticmethod
    def _pair_key(source_id: str, target_id: str) -> tuple[str, str]:
        return (source_id.casefold(), target_id.casefold())

    def _status(
        self,
        source_id: str,
        target_id: str,
        *,
        configured: bool,
        method: DirectAuthMethod | None,
        available: bool | None,
        reason: str | None,
    ) -> DirectAuthStatus:
        status = DirectAuthStatus(
            source_server_id=source_id,
            target_server_id=target_id,
            configured=configured,
            method=method,
            available=available,
            reason=reason,
            checked_at=_now(),
        )
        self._latest_checks[self._pair_key(source_id, target_id)] = status
        return status

    # ---- zero-SSH listing ----------------------------------------------------------------

    def list_for(self, server_id: str) -> DirectAuthList:
        """All configured pairs involving the server; ZERO SSH by contract."""

        pairs: list[DirectAuthPairView] = []
        for record in self._metadata.for_server(server_id):
            key = self._pair_key(record.source_server_id, record.target_server_id)
            latest = self._latest_checks.get(key)
            pairs.append(
                DirectAuthPairView(
                    source_server_id=record.source_server_id,
                    target_server_id=record.target_server_id,
                    configured=True,
                    method=DirectAuthMethod.SGC_KEY,
                    available=latest.available if latest is not None else None,
                    reason=latest.reason if latest is not None else None,
                    checked_at=latest.checked_at if latest is not None else None,
                )
            )
        return DirectAuthList(server_id=server_id, pairs=pairs)

    def dedicated_key_options(self, source_id: str, target_id: str) -> DedicatedKeyOptions | None:
        """SOURCE-side ssh options for the transfer planner (no secrets)."""

        from app.models.transfer import DedicatedKeyOptions

        record = self._metadata.get(source_id, target_id)
        if record is None:
            return None
        return DedicatedKeyOptions(
            private_key_path=record.remote_private_key_path,
            known_hosts_path=record.remote_known_hosts_path,
        )

    def forget_server(self, server_id: str) -> int:
        """Server-deletion bookkeeping: drop local rows mentioning the server.

        Local metadata only — never a remote revoke (the target machine keeps
        its own files; a documented trade-off).
        """
        removed = self._metadata.remove_server(server_id)
        wanted = server_id.casefold()
        for key in [k for k in self._latest_checks if wanted in k]:
            self._latest_checks.pop(key, None)
        return removed

    # ---- explicit SSH check ----------------------------------------------------------------

    async def check(self, source_id: str, target_id: str) -> DirectAuthStatus:
        source = self._server(source_id)
        target = self._server(target_id)
        self._require_enabled([source, target])

        record = self._metadata.get(source_id, target_id)
        configured = record is not None

        missing_rsync = await self._rsync_missing_reason(source, is_source=True)
        if missing_rsync is None:
            missing_rsync = await self._rsync_missing_reason(target, is_source=False)
        if missing_rsync is not None:
            return self._status(
                source_id,
                target_id,
                configured=configured,
                method=None,
                available=False,
                reason=missing_rsync,
            )

        native_ok, native_detail = await self._run_probe(source, target, dedicated_options=None)
        if native_ok:
            return self._status(
                source_id,
                target_id,
                configured=configured,
                method=DirectAuthMethod.NATIVE,
                available=True,
                reason=None,
            )
        if record is None:
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason=_classify_probe_failure(native_detail, dedicated=False),
            )

        options = self._options_from(record)
        dedicated_ok, dedicated_detail = await self._run_probe(
            source, target, dedicated_options=options
        )
        if dedicated_ok:
            return self._status(
                source_id,
                target_id,
                configured=True,
                method=DirectAuthMethod.SGC_KEY,
                available=True,
                reason=None,
            )
        return self._status(
            source_id,
            target_id,
            configured=True,
            method=DirectAuthMethod.SGC_KEY,
            available=False,
            reason=_classify_probe_failure(dedicated_detail, dedicated=True),
        )

    async def _rsync_missing_reason(self, server: ServerRecord, *, is_source: bool) -> str | None:
        try:
            result = await self._ssh.run(server, "command -v rsync", timeout_s=10.0)
        except ExecutorError as exc:
            return _executor_reason(exc)
        if result.exit_code != 0:
            return "rsync_missing_source" if is_source else "rsync_missing_target"
        return None

    async def _run_probe(
        self,
        source: ServerRecord,
        target: ServerRecord,
        *,
        dedicated_options: DedicatedKeyOptions | None,
    ) -> tuple[bool, str]:
        """One bounded BatchMode probe source→target (native or dedicated)."""

        from app.transfer.planner import _batch_mode_probe, dedicated_batch_mode_probe

        params = self._ssh.resolve_params_for(target)
        try:
            session = await self._ssh.transfer_command_session(source)
        except Exception as exc:
            return False, f"probe error: {str(exc)[:120]}"
        try:
            if dedicated_options is None:
                return await _batch_mode_probe(session, params.host, params.port, params.username)
            return await dedicated_batch_mode_probe(
                session, params.host, params.port, params.username, dedicated_options
            )
        except Exception as exc:  # close must never mask the probe outcome
            return False, f"probe error: {str(exc)[:120]}"
        finally:
            with contextlib.suppress(Exception):
                await session.close()

    def _options_from(self, record: DirectAuthPairMetadata) -> DedicatedKeyOptions:
        from app.models.transfer import DedicatedKeyOptions

        return DedicatedKeyOptions(
            private_key_path=record.remote_private_key_path,
            known_hosts_path=record.remote_known_hosts_path,
        )

    # ---- setup-key (spec items 27-28) ------------------------------------------------------

    async def setup_key(self, source_id: str, target_id: str) -> DirectAuthStatus:
        async with self._operation_lock:
            return await self._setup_key_locked(source_id, target_id)

    async def _setup_key_locked(self, source_id: str, target_id: str) -> DirectAuthStatus:
        if source_id.casefold() == target_id.casefold():
            raise ConflictError("source and target servers are the same")
        source = self._server(source_id)
        target = self._server(target_id)
        self._require_enabled([source, target])

        existing = self._metadata.get(source_id, target_id)
        if existing is not None:
            options = self._options_from(existing)
            ok, detail = await self._run_probe(source, target, dedicated_options=options)
            if ok:
                return self._status(
                    source_id,
                    target_id,
                    configured=True,
                    method=DirectAuthMethod.SGC_KEY,
                    available=True,
                    reason="already_configured",
                )
            reason = _classify_probe_failure(detail, dedicated=True)
            if reason != _ROUTE_REASON:
                # Broken (files / authorized line / host trust): repair by
                # revoking ONLY this pair's own materials, then a fresh setup.
                with contextlib.suppress(Exception):
                    await self._remove_pair_materials(source, target, existing)
                with contextlib.suppress(Exception):
                    self._metadata.remove(source_id, target_id)
            else:
                return self._status(
                    source_id,
                    target_id,
                    configured=True,
                    method=DirectAuthMethod.SGC_KEY,
                    available=False,
                    reason=reason,
                )

        key_id = uuid.uuid4().hex[:8]
        private_path = f"{_SOURCE_KEYS_DIR}/sgc-direct-{_slug(source_id)}-{_slug(target_id)}"
        pub_path = f"{private_path}.pub"

        # (1) console → source / target, one bounded `true` each; NOTHING is
        # written before both pass.
        for server in (source, target):
            ok, reason = await self._console_reachable(server)
            if not ok:
                return self._status(
                    source_id,
                    target_id,
                    configured=False,
                    method=None,
                    available=False,
                    reason=reason or _ROUTE_REASON,
                )

        # (2) VERIFIED target host key (no handshake = no setup).
        params = self._ssh.resolve_params_for(target)
        port = params.port if params.port is not None else 22
        try:
            verified = await self._verified_host_key(target)
        except Exception:
            verified = None
        if verified is None:
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason="host_key_unknown",
            )

        try:
            source_session = await self._sftp_session(source)
        except Exception:
            logger.info("source SFTP session unavailable for direct-auth setup")
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason="unknown",
            )
        target_session: TransferSession | None = None
        public_line: str | None = None
        try:
            # (3) app-owned dirs on SOURCE (0700), key only if absent.
            await self._ensure_source_dirs(source_session)
            comment, key_type, blob = await self._obtain_key(
                source, source_session, source_id, target_id, key_id, private_path, pub_path
            )
            fingerprint = _openssh_fingerprint(blob)
            public_line = f"{key_type} {blob}"

            target_session = await self._sftp_session(target)
            try:
                # (5) target authorized_keys: restricted line, append-only.
                await self._ensure_dir(target_session, _SOURCE_SSH_DIR, _DIR_MODE)
                await self._ensure_authorized_line(
                    target_session, public_line=public_line, comment=comment, tag=key_id
                )
                # (6) SOURCE app-owned known_hosts (never the user's file).
                entry = verified.known_hosts_entry(params.host, port)
                await self._ensure_known_hosts_entry(source_session, entry=entry, tag=key_id)

                # (7) the dedicated check decides.
                options = self._options_from_path(private_path)
                ok, detail = await self._run_probe(source, target, dedicated_options=options)
                if not ok:
                    raise _SetupFailure(_classify_probe_failure(detail, dedicated=True))
            except Exception:
                with contextlib.suppress(Exception):
                    if target_session is not None:
                        await target_session.close()
                target_session = None
                raise

            # (8) persist metadata ONLY after the check passed.
            record = DirectAuthPairMetadata(
                source_server_id=source_id,
                target_server_id=target_id,
                key_id=key_id,
                remote_private_key_path=private_path,
                remote_public_key_path=pub_path,
                remote_known_hosts_path=_SOURCE_KNOWN_HOSTS,
                public_key_fingerprint=fingerprint,
                created_at=_now(),
            )
            self._metadata.put(record)
            return self._status(
                source_id,
                target_id,
                configured=True,
                method=DirectAuthMethod.SGC_KEY,
                available=True,
                reason=None,
            )
        except _SetupFailure as failure:
            await self._rollback_attempt(
                source,
                target,
                key_id=key_id,
                private_path=private_path,
                pub_path=pub_path,
                public_line=public_line,
            )
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason=failure.reason,
            )
        except Exception:
            logger.warning("direct-auth setup failed unexpectedly", exc_info=True)
            await self._rollback_attempt(
                source,
                target,
                key_id=key_id,
                private_path=private_path,
                pub_path=pub_path,
                public_line=public_line,
            )
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason="unknown",
            )
        finally:
            with contextlib.suppress(Exception):
                if target_session is not None:
                    await target_session.close()
            with contextlib.suppress(Exception):
                await source_session.close()

    @staticmethod
    def _options_from_path(private_path: str) -> DedicatedKeyOptions:
        from app.models.transfer import DedicatedKeyOptions

        return DedicatedKeyOptions(
            private_key_path=private_path, known_hosts_path=_SOURCE_KNOWN_HOSTS
        )

    async def _verified_host_key(self, server: ServerRecord) -> VerifiedHostKey | None:
        """Transport seam (overridable in offline tests): verified host key."""

        from app.ssh.transport import verified_server_host_key

        return await verified_server_host_key(self._ssh, server)

    async def _console_reachable(self, server: ServerRecord) -> tuple[bool, str]:
        try:
            result = await self._ssh.run(server, "true", timeout_s=10.0)
        except ExecutorError as exc:
            return False, _executor_reason(exc)
        return (result.exit_code == 0, "")

    async def _sftp_session(self, server: ServerRecord) -> TransferSession:
        """Typed SFTP session for one server (manager returns a union type)."""

        from typing import cast

        from app.ssh.file_transfer import TransferSession

        session = await self._ssh.transfer_session(server)
        return cast(TransferSession, session)

    async def _ensure_source_dirs(self, session: TransferSession) -> None:
        ssh_existed = (await session.stat(_SOURCE_SSH_DIR)).exists
        await self._ensure_dir(session, _SOURCE_APP_DIR, _DIR_MODE)
        await self._ensure_dir(session, _SOURCE_KEYS_DIR, _DIR_MODE)
        if not ssh_existed:
            # We created the parent ourselves: make it private as well.
            await session.set_mode(_SOURCE_SSH_DIR, _DIR_MODE)

    async def _ensure_dir(self, session: TransferSession, path: str, mode: int) -> None:
        await session.mkdir(path)
        await session.set_mode(path, mode)

    async def _obtain_key(
        self,
        source: ServerRecord,
        session: TransferSession,
        source_id: str,
        target_id: str,
        attempt_key_id: str,
        private_path: str,
        pub_path: str,
    ) -> tuple[str, str, str]:
        """Ensure the dedicated key exists; returns (comment, key_type, blob).

        Crash-recovery: when the pair's key file already exists (a previous
        attempt died between keygen and metadata persistence), it is REUSED
        and its key_id recovered from the PUBLIC key comment — never a second
        key, never orphan accumulation.
        """

        if (await session.stat(private_path)).exists:
            pub_text = await self._cat_public(source, pub_path)
            key_type, blob, existing_comment = _parse_public_line(pub_text, want_comment=True)
            prefix = f"sgc-direct:{_slug(source_id)}:{_slug(target_id)}:"
            if existing_comment is not None and existing_comment.startswith(prefix):
                recovered = existing_comment[len(prefix) :]
                valid = (
                    recovered
                    and len(recovered) <= 16
                    and all(char in "0123456789abcdef" for char in recovered)
                )
                if valid:
                    comment = _comment(source_id, target_id, recovered)
                    return comment, key_type, blob
            comment = _comment(source_id, target_id, attempt_key_id)
            return comment, key_type, blob

        present, _ = await self._run_fixed(source, "command -v ssh-keygen", timeout_s=10.0)
        if not present:
            raise _SetupFailure("keygen_missing_source")
        key_id = attempt_key_id
        comment = _comment(source_id, target_id, key_id)
        command = (
            f"ssh-keygen -t ed25519 -N {shlex.quote('')}"
            f" -C {shlex.quote(comment)} -f {shlex.quote(private_path)}"
        )
        ok, reason = await self._run_fixed(source, command, timeout_s=30.0)
        if not ok:
            logger.info(
                "ssh-keygen failed on source %s (%s)", source.server_id, reason or "nonzero exit"
            )
            raise _SetupFailure("keygen_missing_source")
        pub_text = await self._cat_public(source, pub_path)
        key_type, blob, _ = _parse_public_line(pub_text, want_comment=False)
        return comment, key_type, blob

    async def _run_fixed(
        self, server: ServerRecord, command: str, *, timeout_s: float
    ) -> tuple[bool, str]:
        try:
            result = await self._ssh.run(server, command, timeout_s=timeout_s)
        except ExecutorError as exc:
            return False, _executor_reason(exc)
        return result.exit_code == 0, ""

    async def _cat_public(self, source: ServerRecord, pub_path: str) -> str:
        try:
            result = await self._ssh.run(source, f"cat {shlex.quote(pub_path)}", timeout_s=10.0)
        except ExecutorError as exc:
            raise _SetupFailure(_executor_reason(exc)) from exc
        if result.exit_code != 0 or not result.stdout.strip():
            raise _SetupFailure("unknown")
        return result.stdout

    async def _ensure_authorized_line(
        self, session: TransferSession, *, public_line: str, comment: str, tag: str
    ) -> bool:
        """Append ONE restricted line; False when the exact comment is present.

        Existing content is preserved byte-for-byte; the file is written via a
        temp file in the same directory (mode 0600 BEFORE the atomic rename —
        never a window with loose permissions on authorized_keys).
        """
        stat_info = await session.stat(_TARGET_AUTH_KEYS)
        existing = b""
        if stat_info.exists:
            existing = await self._read_remote_bytes(session, _TARGET_AUTH_KEYS)
        for line in existing.splitlines():
            if _line_comment(line) == comment:
                return False
        restricted = f"{_RESTRICTED_OPTIONS} {public_line} {comment}".encode("ascii")
        content = _append_line(existing, restricted)
        await self._write_remote_file(
            session, _TARGET_AUTH_KEYS, content, mode=_KEY_FILE_MODE, tag=tag
        )
        return True

    async def _ensure_known_hosts_entry(
        self, session: TransferSession, *, entry: str, tag: str
    ) -> bool:
        """Install the verified key line in the SOURCE app-owned known_hosts.

        The user's own ~/.ssh/known_hosts is NEVER touched — this path is the
        SGC-owned ``~/.ssh/gpu-console/known_hosts`` only.
        """
        stat_info = await session.stat(_SOURCE_KNOWN_HOSTS)
        existing = b""
        if stat_info.exists:
            existing = await self._read_remote_bytes(session, _SOURCE_KNOWN_HOSTS)
        entry_bytes = entry.encode("ascii")
        if any(line.strip() == entry_bytes for line in existing.splitlines()):
            return False
        content = _append_line(existing, entry_bytes)
        await self._write_remote_file(
            session, _SOURCE_KNOWN_HOSTS, content, mode=_KEY_FILE_MODE, tag=tag
        )
        return True

    @staticmethod
    async def _read_remote_bytes(session: TransferSession, path: str) -> bytes:
        handle = await session.open_reader(path)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = await handle.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            with contextlib.suppress(Exception):
                await handle.close()
        return b"".join(chunks)

    async def _remove_authorized_line(
        self,
        session: TransferSession,
        *,
        comment: str,
        fingerprint: str | None,
        tag: str,
    ) -> bool:
        """Remove the FIRST authorized_keys line with this exact comment.

        Revocation never touches user material: the line is removed only when
        its comment matches AND (when a fingerprint is known) the key blob's
        fingerprint matches the stored one — a comment that survives with a
        DIFFERENT key is left untouched (it may be user-modified). Absent
        line → False (already-revoked, never an error). Read failures raise:
        the file is only rewritten from content we actually read.
        """
        stat_info = await session.stat(_TARGET_AUTH_KEYS)
        if not stat_info.exists:
            return False
        existing = await self._read_remote_bytes(session, _TARGET_AUTH_KEYS)
        kept: list[bytes] = []
        removed = False
        for line in existing.splitlines(keepends=True):
            if not removed and _line_comment(line) == comment:
                blob = _line_blob(line)
                if fingerprint is not None and (
                    blob is None or _openssh_fingerprint(blob) != fingerprint
                ):
                    kept.append(line)  # comment matches but the key differs: keep
                    continue
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        await self._write_remote_file(
            session, _TARGET_AUTH_KEYS, b"".join(kept), mode=_KEY_FILE_MODE, tag=tag
        )
        return True

    @staticmethod
    async def _write_remote_file(
        session: TransferSession, path: str, data: bytes, *, mode: int, tag: str
    ) -> None:
        directory = posixpath.dirname(path) or "."
        temp = posixpath.join(directory, f".sgc-tmp-{tag}-{uuid.uuid4().hex[:8]}")
        try:
            handle = await session.open_writer(temp)
            try:
                await handle.write(data)
            finally:
                with contextlib.suppress(Exception):
                    await handle.close()
            await session.set_mode(temp, mode)
            await session.rename(temp, path)
        except BaseException:
            with contextlib.suppress(Exception):
                await session.remove(temp)
            raise

    # ---- rollback / revoke -------------------------------------------------------------------

    async def _rollback_attempt(
        self,
        source: ServerRecord,
        target: ServerRecord,
        *,
        key_id: str,
        private_path: str,
        pub_path: str,
        public_line: str | None = None,
    ) -> None:
        """Best-effort removal of ONLY this attempt's materials (user content untouched).

        When the public key line of the attempt is known (all failures after
        the key read), the attempt's known_hosts line is removed as well; for
        earlier failures nothing was ever written there.
        """

        record = DirectAuthPairMetadata(
            source_server_id=source.server_id,
            target_server_id=target.server_id,
            key_id=key_id,
            remote_private_key_path=private_path,
            remote_public_key_path=pub_path,
            remote_known_hosts_path=_SOURCE_KNOWN_HOSTS,
            public_key_fingerprint="",
            created_at=_now(),
        )
        with contextlib.suppress(Exception):
            await self._remove_pair_materials(
                source,
                target,
                record,
                remove_known_hosts=public_line is not None,
                known_hosts_blob=(public_line.split()[1] if public_line else None),
            )

    async def _remove_pair_materials(
        self,
        source: ServerRecord,
        target: ServerRecord,
        record: DirectAuthPairMetadata,
        *,
        remove_known_hosts: bool = False,
        known_hosts_blob: str | None = None,
    ) -> list[str]:
        """Remove the pair's authorized line + key files. Returns failure notes.

        The app-owned known_hosts entry is deliberately KEPT on revoke
        (documented): a stale entry never weakens the security posture
        (OpenSSH accepts when ANY line matches the presented key). A FAILED
        SETUP attempt, however, rolls its own entry back (remove_known_hosts
        + the attempt's key blob).
        """
        failures: list[str] = []
        try:
            target_session = await self._sftp_session(target)
        except Exception:
            failures.append("target session could not be opened")
        else:
            try:
                await self._remove_authorized_line(
                    target_session,
                    comment=_comment(
                        record.source_server_id, record.target_server_id, record.key_id
                    ),
                    fingerprint=record.public_key_fingerprint or None,
                    tag=record.key_id,
                )
            except Exception:
                failures.append("authorized_keys line could not be removed")
            finally:
                with contextlib.suppress(Exception):
                    await target_session.close()

        try:
            source_session = await self._sftp_session(source)
        except Exception:
            failures.append("source session could not be opened")
        else:
            try:
                for path in (record.remote_private_key_path, record.remote_public_key_path):
                    try:
                        await source_session.remove(path)
                    except Exception:
                        if (await source_session.stat(path)).exists:
                            failures.append(f"{posixpath.basename(path)} could not be removed")
                if remove_known_hosts and known_hosts_blob:
                    await self._remove_known_hosts_blob(
                        source_session, blob=known_hosts_blob, tag=record.key_id
                    )
            except Exception:
                failures.append("source materials could not be fully removed")
            finally:
                with contextlib.suppress(Exception):
                    await source_session.close()
        return failures

    async def _remove_known_hosts_blob(
        self, session: TransferSession, *, blob: str, tag: str
    ) -> bool:
        """Drop the attempt's own entry from the SGC-owned known_hosts file.

        Only lines carrying this attempt's public key blob are removed; other
        entries (other pairs, rotated keys) are preserved byte-for-byte.
        """
        stat_info = await session.stat(_SOURCE_KNOWN_HOSTS)
        if not stat_info.exists:
            return False
        existing = await self._read_remote_bytes(session, _SOURCE_KNOWN_HOSTS)
        kept: list[bytes] = []
        removed = False
        for line in existing.splitlines(keepends=True):
            fields = _line_fields(line)
            if not removed and fields is not None and blob in fields[1:]:
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        await self._write_remote_file(
            session, _SOURCE_KNOWN_HOSTS, b"".join(kept), mode=_KEY_FILE_MODE, tag=tag
        )
        return True

    async def revoke(self, source_id: str, target_id: str) -> DirectAuthStatus:
        async with self._operation_lock:
            return await self._revoke_locked(source_id, target_id)

    async def _revoke_locked(self, source_id: str, target_id: str) -> DirectAuthStatus:
        record = self._metadata.get(source_id, target_id)
        if record is None:
            # Already revoked (or never configured): safe to repeat, not an error.
            return DirectAuthStatus(
                source_server_id=source_id,
                target_server_id=target_id,
                configured=False,
                method=None,
                available=None,
                reason=None,
                checked_at=_now(),
            )
        try:
            source = self._server(source_id)
            target = self._server(target_id)
        except NotFoundError:
            # Server gone (or ids invalid): local bookkeeping only.
            self._metadata.remove(source_id, target_id)
            return DirectAuthStatus(
                source_server_id=source_id,
                target_server_id=target_id,
                configured=False,
                method=None,
                available=None,
                reason=None,
                checked_at=_now(),
            )
        failures = await self._remove_pair_materials(source, target, record)
        if failures:
            # NEVER claim configured afterwards; metadata stays for a retry.
            return self._status(
                source_id,
                target_id,
                configured=False,
                method=None,
                available=False,
                reason="; ".join(failures),
            )
        self._metadata.remove(source_id, target_id)
        return self._status(
            source_id,
            target_id,
            configured=False,
            method=None,
            available=None,
            reason=None,
        )


def _append_line(existing: bytes, line: bytes) -> bytes:
    base = existing if (not existing or existing.endswith(b"\n")) else existing + b"\n"
    return base + line + b"\n"


def _parse_public_line(text: str, *, want_comment: bool) -> tuple[str, str, str | None]:
    """Parse the one-line OpenSSH public key "<type> <b64> [comment]"."""

    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise _SetupFailure("unknown")
    fields = lines[-1].split()
    if len(fields) < 2:
        raise _SetupFailure("remote_key_invalid")
    key_type, blob = fields[0], fields[1]
    try:
        base64.b64decode(blob, validate=True)
    except ValueError as error:
        raise _SetupFailure("remote_key_invalid") from error
    comment = fields[2] if want_comment and len(fields) >= 3 else None
    return key_type, blob, comment

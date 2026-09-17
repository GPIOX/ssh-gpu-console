"""In-memory server registry with explicit atomic persistence.

Writes happen only on explicit CRUD mutations. Duplicates are detected on the
casefolded ``(ssh_host, username, port)`` triple; hosts are validated against
shell-hostile characters at the CRUD boundary (defense in depth — hosts are
never concatenated into shell strings by the transport anyway).
"""

from __future__ import annotations

import re
import threading
import uuid
from typing import Any

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.server import ServerCreate, ServerPatch, ServerRecord
from app.persistence.json_store import JsonFileStore

logger = get_logger("servers.registry")

_MAX_SERVERS = 256
_FORBIDDEN_HOST_CHARS = frozenset(" ;&|<>`$\\\"'()/\t\n\r*?!")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_host(value: str) -> str:
    candidate = value.strip()
    if not candidate or len(candidate) > 255 or candidate.startswith("-"):
        raise ValueError("invalid SSH host or alias")
    if any(char in _FORBIDDEN_HOST_CHARS for char in candidate):
        raise ValueError("invalid SSH host or alias")
    return candidate


def _validate_username(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 64 or not _USERNAME_RE.fullmatch(candidate):
        raise ValueError("invalid username")
    return candidate


def _new_server_id() -> str:
    return uuid.uuid4().hex[:12]


def _validated_record(record: ServerRecord) -> ServerRecord:
    _validate_host(record.ssh_host)
    _validate_username(record.username)
    return record


class ServerRegistry:
    def __init__(self, store: JsonFileStore) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._servers: dict[str, ServerRecord] = {}
        self.revision = 0
        self._load()

    def _load(self) -> None:
        raw: Any = self._store.load(default=[])
        if not isinstance(raw, list):
            logger.warning("registry JSON root is not a list; starting empty")
            return
        for record in raw:
            if not isinstance(record, dict):
                continue
            try:
                server = ServerRecord.model_validate(record)
            except Exception:  # one bad record must not brick startup
                logger.warning("skipping invalid server registry record", exc_info=True)
                continue
            self._servers[server.server_id] = server

    def _persist_locked(self) -> None:
        payload = [server.model_dump(mode="json") for server in self._servers.values()]
        self._store.save(payload)

    def all(self) -> list[ServerRecord]:
        with self._lock:
            ordered = sorted(self._servers.values(), key=lambda item: item.display_name.casefold())
            return [server.model_copy(deep=True) for server in ordered]

    def get(self, server_id: str) -> ServerRecord:
        with self._lock:
            server = self._servers.get(server_id)
            if server is None:
                raise NotFoundError(f"server {server_id!r} not found")
            return server.model_copy(deep=True)

    def get_enabled(self) -> list[ServerRecord]:
        return [server for server in self.all() if server.enabled]

    def count(self) -> int:
        with self._lock:
            return len(self._servers)

    def create(self, request: ServerCreate) -> ServerRecord:
        record = _validated_record(ServerRecord(server_id=_new_server_id(), **request.model_dump()))
        with self._lock:
            if len(self._servers) >= _MAX_SERVERS:
                raise ConflictError(f"server limit of {_MAX_SERVERS} reached")
            if self._duplicate(record, exclude=None):
                raise ConflictError("a server with the same host, username and port already exists")
            self._servers[record.server_id] = record
            self._persist_locked()
            self.revision += 1
        logger.info("server added: %s (%s)", record.display_name, record.ssh_host)
        return record.model_copy(deep=True)

    def update(self, server_id: str, request: ServerPatch) -> ServerRecord:
        with self._lock:
            current = self._servers.get(server_id)
            if current is None:
                raise NotFoundError(f"server {server_id!r} not found")
            patch = request.model_dump(exclude_unset=True)
            candidate = _validated_record(
                ServerRecord.model_validate(
                    {**current.model_dump(mode="python"), **patch, "server_id": current.server_id}
                )
            )
            if self._duplicate(candidate, exclude=server_id):
                raise ConflictError("a server with the same host, username and port already exists")
            self._servers[server_id] = candidate
            self._persist_locked()
            self.revision += 1
        logger.info("server updated: %s", server_id)
        return candidate.model_copy(deep=True)

    def delete(self, server_id: str) -> None:
        with self._lock:
            if server_id not in self._servers:
                raise NotFoundError(f"server {server_id!r} not found")
            self._servers.pop(server_id)
            self._persist_locked()
            self.revision += 1
        logger.info("server deleted: %s", server_id)

    @staticmethod
    def _key(server: ServerRecord) -> tuple[str, str | None, int | None]:
        return server.ssh_host.casefold(), server.username, server.port

    def _duplicate(self, candidate: ServerRecord, *, exclude: str | None) -> bool:
        key = self._key(candidate)
        return any(
            server_id != exclude and self._key(item) == key
            for server_id, item in self._servers.items()
        )


_default_registry: ServerRegistry | None = None
_registry_lock = threading.Lock()


def get_default_registry() -> ServerRegistry:
    """Lazy singleton used by the composition root and API routes.

    The data directory comes from ``Settings`` (``SGC_DATA_DIR`` env override
    honored); tests install their own instance via ``set_default_registry``.
    """

    global _default_registry
    with _registry_lock:
        if _default_registry is None:
            from app.core.config import Settings

            _default_registry = ServerRegistry(JsonFileStore(Settings().data_dir / "registry.json"))
        return _default_registry


def set_default_registry(registry: ServerRegistry | None) -> None:
    global _default_registry
    with _registry_lock:
        _default_registry = registry

"""Persistence of configured direct-auth pairs (Phase 4.2C).

One small JSON file (`direct_auth.json`) over the atomic JsonFileStore —
written ONLY on explicit setup/revoke (and the server-deletion bookkeeping
hook), never periodically, never by a check. A missing or malformed file
degrades to "no pairs configured"; records are validated individually so one
bad row cannot brick the domain.
"""

from __future__ import annotations

import threading

from app.core.logging import get_logger
from app.models.direct_auth import DirectAuthPairMetadata
from app.persistence.json_store import JsonFileStore

logger = get_logger("direct_auth.metadata")

_PAIR = tuple[str, str]


class DirectAuthMetadata:
    """In-memory view over the dedicated-key pair store (explicit writes only)."""

    def __init__(self, store: JsonFileStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._records: list[DirectAuthPairMetadata] = []
        self._load()

    def _load(self) -> None:
        raw = self._store.load(default=[])
        if not isinstance(raw, list):
            logger.warning(
                "direct-auth store %s has unexpected shape; starting empty", self._store.path
            )
            raw = []
        records: list[DirectAuthPairMetadata] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                records.append(DirectAuthPairMetadata.model_validate(item))
            except Exception:
                logger.warning("skipping invalid direct-auth record", exc_info=True)
        self._records = records

    def _persist(self) -> None:
        self._store.save([record.model_dump(mode="json") for record in self._records])

    @staticmethod
    def _key(source_server_id: str, target_server_id: str) -> tuple[str, str]:
        return (source_server_id.casefold(), target_server_id.casefold())

    def get(self, source_server_id: str, target_server_id: str) -> DirectAuthPairMetadata | None:
        key = self._key(source_server_id, target_server_id)
        return next(
            (
                record
                for record in self._records
                if self._key(record.source_server_id, record.target_server_id) == key
            ),
            None,
        )

    def for_server(self, server_id: str) -> list[DirectAuthPairMetadata]:
        """All pairs where the server is source OR target (preserves order)."""
        wanted = server_id.casefold()
        return [
            record
            for record in self._records
            if wanted in (record.source_server_id.casefold(), record.target_server_id.casefold())
        ]

    def put(self, record: DirectAuthPairMetadata) -> None:
        """Upsert one pair record (one row per (source, target), newest wins)."""
        key = self._key(record.source_server_id, record.target_server_id)
        with self._lock:
            self._records = [
                r for r in self._records if self._key(r.source_server_id, r.target_server_id) != key
            ] + [record]
            self._persist()

    def remove(self, source_server_id: str, target_server_id: str) -> bool:
        """Drop one pair record; returns True when a row existed."""
        key = self._key(source_server_id, target_server_id)
        with self._lock:
            before = len(self._records)
            self._records = [
                r for r in self._records if self._key(r.source_server_id, r.target_server_id) != key
            ]
            removed = len(self._records) < before
            if removed:
                self._persist()
        return removed

    def remove_server(self, server_id: str) -> int:
        """Local bookkeeping for server deletion: drop every row mentioning it."""
        wanted = server_id.casefold()
        with self._lock:
            remaining = [
                r
                for r in self._records
                if wanted not in (r.source_server_id.casefold(), r.target_server_id.casefold())
            ]
            removed = len(self._records) - len(remaining)
            self._records = remaining
            if removed:
                self._persist()
        return removed

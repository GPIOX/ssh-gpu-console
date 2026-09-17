"""Host-key trust store: app-owned TOFU consent, persisted as JSON.

Security model:

- The user's ``~/.ssh/known_hosts`` is verified natively by AsyncSSH and is
  NEVER written by this application.
- Keys absent from the user's known_hosts are surfaced to the user as a
  fingerprint prompt; connection proceeds only after explicit consent.
- Consent is persisted here (``backend/data/trusted_host_keys.json`` via the
  atomic JSON store) as ``{host, port, key_type, fingerprint}`` entries; the
  transport accepts a presented key when its SHA256 fingerprint matches an
  entry for that host and port.
- A key presented for a host that already has trusted entries (user file or
  trust store) is a mismatch: a security event with no trust path.

Fingerprints are compared exactly (after whitespace strip): the base64 digest
is case-sensitive, so lowering either side could accept a different key.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.persistence.json_store import JsonFileStore

logger = get_logger("ssh.known_hosts")

TRUST_FILE_NAME = "trusted_host_keys.json"
_STORE_VERSION = 1


@dataclass(frozen=True, slots=True)
class TrustedHostKey:
    """One explicitly trusted host key, identified by fingerprint."""

    host: str
    port: int
    key_type: str
    fingerprint: str


class HostKeyTrust:
    """In-memory view over the app-owned host-key trust file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: list[TrustedHostKey] = []
        self._load()

    def _load(self) -> None:
        raw: Any = JsonFileStore(self._path).load(default=[])
        entries: list[TrustedHostKey] = []
        if isinstance(raw, dict):
            raw = raw.get("entries", [])
        if not isinstance(raw, list):
            logger.warning(
                "host-key trust file %s has unexpected shape; starting empty", self._path
            )
            raw = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    TrustedHostKey(
                        host=str(item["host"]).casefold(),
                        port=int(item["port"]),
                        key_type=str(item["key_type"]),
                        fingerprint=str(item["fingerprint"]).strip(),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("skipping invalid host-key trust entry")
                continue
        self._entries = entries

    def entries(self) -> tuple[TrustedHostKey, ...]:
        with self._lock:
            return tuple(self._entries)

    def is_trusted(self, host: str, port: int | None, fingerprint: str) -> bool:
        wanted = (host.casefold(), port if port is not None else 22, fingerprint.strip())
        with self._lock:
            return any(
                (entry.host, entry.port, entry.fingerprint) == wanted for entry in self._entries
            )

    def has_host(self, host: str, port: int | None) -> bool:
        """Whether ANY key is trusted for this host/port (mismatch detection)."""

        wanted = (host.casefold(), port if port is not None else 22)
        with self._lock:
            return any((entry.host, entry.port) == wanted for entry in self._entries)

    def add(self, entry: TrustedHostKey) -> None:
        """Persist one consent decision; duplicate entries are ignored."""

        normalized = TrustedHostKey(
            host=entry.host.casefold(),
            port=int(entry.port),
            key_type=entry.key_type,
            fingerprint=entry.fingerprint.strip(),
        )
        with self._lock:
            if normalized in self._entries:
                return
            self._entries.append(normalized)
            self._persist_locked()
        logger.info(
            "host key trusted for %s:%d (%s)", normalized.host, normalized.port, normalized.key_type
        )

    def _persist_locked(self) -> None:
        payload = {
            "version": _STORE_VERSION,
            "entries": [asdict(entry) for entry in self._entries],
        }
        JsonFileStore(self._path).save(payload)

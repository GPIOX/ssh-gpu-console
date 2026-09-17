"""Read-only SSH config alias discovery for onboarding and fleet display.

The parsed view hot-reloads when the config's mtime changes (reparse from
scratch so edits and included files are picked up), folds aliases that
resolve to the same ``(hostname, port, user)`` into one canonical entry, and
keeps a user-driven ignore/unignore list. Discovery never registers servers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from app.core.logging import get_logger
from app.ssh.config_resolver import SSHConfigResolver

logger = get_logger("servers.discovery")


@dataclass(frozen=True)
class DiscoveredServer:
    """One canonical entry from the user's SSH config."""

    alias: str
    display_name: str
    hostname: str | None
    username: str | None
    port: int | None
    aliases: tuple[str, ...] = ()
    ignored: bool = False


class SSHConfigDiscovery:
    def __init__(self, resolver: SSHConfigResolver | None = None) -> None:
        self._resolver = resolver or SSHConfigResolver()
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._servers: dict[str, DiscoveredServer] = {}
        self._ignored: set[str] = set()
        self._revision = 0

    @property
    def resolver(self) -> SSHConfigResolver:
        return self._resolver

    def _mtime(self) -> int:
        try:
            return self._resolver.path.stat().st_mtime_ns
        except OSError:
            return -1  # missing file: rescan once it appears

    def scan(self) -> None:
        current_mtime = self._mtime()
        with self._lock:
            if current_mtime == self._mtime_ns:
                return
            self._mtime_ns = current_mtime
            # Reparse from scratch to pick up edits and included files.
            self._resolver = SSHConfigResolver(self._resolver.path)
            parsed: dict[str, DiscoveredServer] = {}
            canonical: dict[tuple[str, int, str | None], str] = {}
            for alias in self._resolver.list_aliases_ordered():
                entry = self._resolver.resolve(alias)
                key = (entry.hostname or alias, entry.port or 22, entry.user)
                existing_alias = canonical.get(key)
                if existing_alias is not None:
                    previous = parsed[existing_alias]
                    parsed[existing_alias] = replace(
                        previous,
                        aliases=(*previous.aliases, alias),
                        ignored=previous.ignored or alias in self._ignored,
                    )
                    continue
                canonical[key] = alias
                parsed[alias] = DiscoveredServer(
                    alias=alias,
                    display_name=alias,
                    hostname=entry.hostname,
                    username=entry.user,
                    port=entry.port,
                    ignored=alias in self._ignored,
                )
            self._servers = parsed
            self._revision += 1
            logger.info("discovered %d SSH aliases", len(parsed))

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def aliases(self) -> list[DiscoveredServer]:
        with self._lock:
            return list(self._servers.values())

    def ignore(self, alias: str) -> None:
        with self._lock:
            self._ignored.add(alias)
            if alias in self._servers:
                self._servers[alias] = replace(self._servers[alias], ignored=True)

    def unignore(self, alias: str) -> None:
        with self._lock:
            self._ignored.discard(alias)
            if alias in self._servers:
                # Also refresh the current entry: the mtime may not have
                # changed, so a scan alone would keep the stale flag.
                self._servers[alias] = replace(self._servers[alias], ignored=False)


_default_discovery: SSHConfigDiscovery | None = None
_discovery_lock = threading.Lock()


def get_default_discovery() -> SSHConfigDiscovery:
    """Lazy singleton over the user's ``~/.ssh/config`` (tests may override)."""

    global _default_discovery
    with _discovery_lock:
        if _default_discovery is None:
            _default_discovery = SSHConfigDiscovery()
        return _default_discovery


def set_default_discovery(discovery: SSHConfigDiscovery | None) -> None:
    global _default_discovery
    with _discovery_lock:
        _default_discovery = discovery

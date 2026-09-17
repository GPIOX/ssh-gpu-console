"""Read-only OpenSSH config parsing: alias discovery and effective-value resolution.

Supported subset: ``Host`` blocks with the directives this product consumes,
``Include`` (with glob expansion and a cycle guard), negated ``!patterns``,
first-value-wins semantics, ``%h``/``%n``/``%d`` expansion, whitespace-preceded
inline comments, and OpenSSH's ``Keyword=value`` syntax. ``Match`` blocks are
not evaluated; their directives are ignored entirely and never attributed to
the preceding ``Host`` block. ``ProxyJump`` chains are resolved hop-by-hop
through this same config (aliases → concrete ``[user@]host:port``, nested
jumps prepend, cycles rejected) because the transport's SSH library resolves
tunnel strings by DNS alone. The config is never written.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import shlex
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger
from app.ssh.errors import ConnectError, SshErrorCode

logger = get_logger("ssh.config")

# OpenSSH also accepts ``Keyword=value`` (no whitespace around '=').
_KV_SYNTAX = re.compile(r"^([A-Za-z][A-Za-z0-9]*)=(.*)$")


@dataclass(frozen=True)
class SSHConfigEntry:
    name: str
    hostname: str | None = None
    user: str | None = None
    port: int | None = None
    identity_files: tuple[Path, ...] = ()
    proxy_jump: str | None = None


@dataclass
class ConnectParams:
    """Effective connection parameters after ssh_config + registry overrides."""

    host: str
    port: int = 22
    username: str | None = None
    identity_files: list[Path] = field(default_factory=list)
    proxy_jump: str | None = None
    source: str = "default"
    config_path: Path | None = None
    original_host: str | None = None


def _strip_inline_comment(value: str) -> str:
    # '#' is valid inside aliases and paths. Treat it as a comment start only
    # when preceded by whitespace or at the beginning of the token.
    for index, char in enumerate(value):
        if char == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value.strip()


def _split_directive(line: str) -> tuple[str, list[str]] | None:
    """Parse one config line into ``(keyword, args)`` in both supported syntaxes."""

    kv = _KV_SYNTAX.match(line)
    if kv is not None:
        remainder = _strip_inline_comment(kv.group(2))
        try:
            return kv.group(1).casefold(), shlex.split(remainder, comments=False, posix=True)
        except ValueError:
            return None
    try:
        pieces = shlex.split(line, comments=False, posix=True)
    except ValueError:
        return None
    if not pieces:
        return None
    return pieces[0].casefold(), pieces[1:]


class SSHConfigResolver:
    """Parse the useful subset of ssh_config without ever writing to it."""

    _DIRECTIVES = frozenset({"hostname", "user", "port", "identityfile", "proxyjump"})

    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or Path.home() / ".ssh" / "config").expanduser()
        self._blocks: list[tuple[list[str], dict[str, list[str]]]] = []
        self._loaded_files: set[Path] = set()
        self._load_file(self.path)

    def _load_file(self, path: Path) -> None:
        path = path.expanduser().resolve(strict=False)
        if path in self._loaded_files:  # Include cycles stop here.
            return
        self._loaded_files.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        patterns: list[str] = []
        values: dict[str, list[str]] = {}
        in_block = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parsed = _split_directive(line)
            if parsed is None:
                logger.warning("skipping malformed SSH config line in %s", path)
                continue
            key, args = parsed
            if key == "host":
                if in_block:
                    self._blocks.append((patterns, values))
                patterns = [piece for piece in (_strip_inline_comment(a) for a in args) if piece]
                values = {}
                in_block = True
                continue
            if key == "match":
                # Match criteria are not evaluated. Finalize the current block
                # and drop directives until the next Host line.
                if in_block:
                    self._blocks.append((patterns, values))
                patterns, values, in_block = [], {}, False
                continue
            if key == "include":
                for pattern in args:
                    self._include(path, pattern)
                continue
            if not in_block or key not in self._DIRECTIVES or not args:
                continue
            values.setdefault(key, []).append(_strip_inline_comment(" ".join(args)))
        if in_block:
            self._blocks.append((patterns, values))

    def _include(self, referring_file: Path, pattern: str) -> None:
        include_path = Path(os.path.expandvars(os.path.expanduser(pattern)))
        if not include_path.is_absolute():
            include_path = referring_file.parent / include_path
        for matched in sorted(glob.glob(str(include_path))):
            self._load_file(Path(matched))

    @staticmethod
    def _matches(patterns: list[str], alias: str) -> bool:
        # OpenSSH matches Host patterns case-insensitively (hostnames are
        # case-insensitive in DNS); usernames are not part of this matching.
        subject = alias.casefold()
        positive = False
        for pattern in patterns:
            negated = pattern.startswith("!")
            expression = (pattern[1:] if negated else pattern).casefold()
            if fnmatch.fnmatchcase(subject, expression):
                if negated:
                    return False
                positive = True
        return positive

    def matches(self, alias: str) -> bool:
        return any(self._matches(patterns, alias) for patterns, _ in self._blocks)

    def resolve(self, alias: str) -> SSHConfigEntry:
        hostname: str | None = None
        user: str | None = None
        port: int | None = None
        proxy_jump: str | None = None
        identity_files: list[Path] = []

        for patterns, values in self._blocks:
            if not self._matches(patterns, alias):
                continue
            # First matching value wins, so more specific earlier blocks hold.
            if hostname is None and values.get("hostname"):
                hostname = self._expand(values["hostname"][0], alias)
            if user is None and values.get("user"):
                user = self._expand(values["user"][0], alias)
            if port is None and values.get("port"):
                try:
                    candidate = int(values["port"][0])
                except ValueError:
                    candidate = 0
                if 1 <= candidate <= 65535:
                    port = candidate
            if proxy_jump is None and values.get("proxyjump"):
                proxy_jump = self._expand(values["proxyjump"][0], alias)
            for raw_identity in values.get("identityfile", []):
                identity = Path(self._expand_path(raw_identity, alias))
                if identity not in identity_files:
                    identity_files.append(identity)

        return SSHConfigEntry(
            name=alias,
            hostname=hostname,
            user=user,
            port=port,
            identity_files=tuple(identity_files),
            proxy_jump=proxy_jump,
        )

    def list_aliases_ordered(self) -> list[str]:
        aliases: list[str] = []
        for patterns, _ in self._blocks:
            for pattern in patterns:
                # Wildcard/negated patterns are not usable aliases for onboarding.
                if pattern.startswith("!") or any(char in pattern for char in "*?!"):
                    continue
                if pattern not in aliases:
                    aliases.append(pattern)
        return aliases

    def list_aliases(self) -> list[str]:
        return sorted(self.list_aliases_ordered(), key=str.casefold)

    @staticmethod
    def _expand(value: str, alias: str) -> str:
        return value.replace("%h", alias).replace("%n", alias).strip()

    @classmethod
    def _expand_path(cls, value: str, alias: str) -> str:
        expanded = cls._expand(value, alias)
        expanded = expanded.replace("%d", str(Path.home()))
        return os.path.expandvars(os.path.expanduser(expanded))


def _split_hop(token: str) -> tuple[str | None, str, int | None]:
    """Split one ProxyJump hop ``[user@]host[:port]`` into its parts.

    Bracketed IPv6 (``[fd00::1]:22``) is supported; a trailing colon that
    cannot terminate a port (bare IPv6 without brackets) stays in the host.
    """

    user: str | None = None
    rest = token
    if "@" in rest:
        before, _, after = rest.rpartition("@")
        user = before or None
        rest = after
    if rest.startswith("["):
        close = rest.find("]")
        if close == -1:
            return user, rest, None
        host = rest[1:close]
        tail = rest[close + 1 :]
        port = int(tail[1:]) if tail.startswith(":") and tail[1:].isdigit() else None
        return user, host, port
    candidate, sep, port_str = rest.rpartition(":")
    if sep and candidate and port_str.isdigit():
        return user, candidate, int(port_str)
    return user, rest, None


def resolve_proxy_jump(
    spec: str, resolver: SSHConfigResolver, seen: frozenset[str] = frozenset()
) -> str:
    """Expand a ProxyJump spec into concrete ``[user@]host[:port]`` hops.

    asyncssh resolves tunnel hop strings by DNS alone (its ssh_config load is
    disabled), so every hop alias must be substituted here before the spec is
    handed to the transport. Nested ProxyJump entries prepend their own hops
    (deepest first), matching OpenSSH's left-to-right hop order. ``none`` is
    dropped; concrete addresses pass through.
    """

    hops: list[str] = []
    for raw in spec.split(","):
        token = raw.strip()
        if not token or token.casefold() == "none":
            continue
        user, host, port = _split_hop(token)
        entry = resolver.resolve(host)
        effective_host = entry.hostname or host
        effective_user = user if user is not None else entry.user
        effective_port = port if port is not None else (entry.port or 22)
        if effective_host.casefold() in seen:
            raise ValueError(f"proxy jump cycle detected at {effective_host!r}")
        effective_seen = seen | {effective_host.casefold()}
        hop = f"{effective_user}@" if effective_user else ""
        if ":" in effective_host and effective_port != 22:
            hop += f"[{effective_host}]:{effective_port}"
        else:
            hop += effective_host
            if effective_port != 22:
                hop += f":{effective_port}"
        inner_resolved = (
            resolve_proxy_jump(entry.proxy_jump, resolver, effective_seen)
            if entry.proxy_jump
            else ""
        )
        hops = (inner_resolved.split(",") if inner_resolved else []) + hops + [hop]
    return ",".join(hops)


def resolve_connect_params(
    server_host: str,
    resolver: SSHConfigResolver | None = None,
    *,
    username: str | None = None,
    port: int | None = None,
) -> ConnectParams:
    """Apply explicit registry overrides over ssh_config, then defaults."""

    active_resolver = resolver or SSHConfigResolver()
    entry = active_resolver.resolve(server_host)
    host = entry.hostname or server_host
    effective_username = username if username is not None else entry.user
    effective_port = port if port is not None else (entry.port or 22)
    existing_keys = [path for path in entry.identity_files if path.is_file()]
    source = (
        "ssh_config"
        if (entry.hostname or entry.user or entry.port or entry.identity_files or entry.proxy_jump)
        else "default"
    )
    jump_chain: str | None = entry.proxy_jump
    if jump_chain is not None:
        try:
            jump_chain = resolve_proxy_jump(jump_chain, active_resolver) or None
        except ValueError as error:
            raise ConnectError(SshErrorCode.UNKNOWN, str(error), retryable=False) from error
    return ConnectParams(
        host=host,
        port=effective_port,
        username=effective_username,
        identity_files=existing_keys,
        proxy_jump=jump_chain,
        source=source,
        config_path=active_resolver.path,
        original_host=server_host,
    )


def iter_ssh_config_aliases(path: Path | None = None) -> Iterable[str]:
    return SSHConfigResolver(path).list_aliases()

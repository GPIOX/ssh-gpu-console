"""Safe POSIX path components for artifact display names.

Artifact display ``name``/``version`` (e.g. "IVMSD" / "v1") must never be
interpolated into remote paths as-is: display text may contain slashes,
shell metacharacters or control bytes. These helpers turn display text into
deterministic, readable path components and are the single source of truth
for Project Sync target-path generation. Case is preserved for readability;
results depend only on input order and content (no ``hash()``), so they are
stable across processes.
"""

from __future__ import annotations

import posixpath
import re
import string
import unicodedata

from app.core.errors import ConflictError

# ASCII characters safe to keep verbatim in a POSIX filename component.
_SAFE_ASCII = frozenset(string.ascii_letters + string.digits + "._-")
_DASH_RUN = re.compile("-{2,}")


def _is_kept(char: str) -> bool:
    """ASCII-safe characters plus non-ASCII letters/numbers stay readable."""
    if char in _SAFE_ASCII:
        return True
    return unicodedata.category(char)[0] in ("L", "N")


def sanitize_component(text: str, *, max_length: int = 80) -> str:
    """Turn arbitrary display text into one safe POSIX path component.

    Slashes, backslash, NUL/CR/LF and every control/format codepoint
    (categories Cc/Cf, or ord < 0x20 / 0x7f) become "-"; the same happens to
    shell-hostile punctuation (``:``, quotes, ``; & | $ ( ) < > * ? ~ ! # %
    { } [ ]``) and whitespace. Runs of "-" collapse to one; leading and
    trailing "-" and "." are stripped. Empty input, input left without any
    usable character, or an all-dot result raises ConflictError. Length is
    capped at ``max_length`` and never ends in "-" or ".". Case is kept.
    """
    if max_length < 1:
        raise ValueError("max_length must be at least 1")
    candidate = text.strip()
    if not candidate:
        raise ConflictError("artifact path component is empty")
    # Control bytes and unsafe punctuation both normalize to "-", so a
    # single keep-or-dash pass realizes rules 2 and 3 of the contract.
    cleaned = "".join(char if _is_kept(char) else "-" for char in candidate)
    cleaned = _DASH_RUN.sub("-", cleaned).strip("-.")
    if not cleaned or set(cleaned) == {"."}:
        raise ConflictError(f"no usable path characters in {text!r}")
    if len(cleaned) > max_length:
        # cleaned[0] is never "-" or "." here, so truncation plus the
        # trailing strip cannot produce an empty or all-dot component.
        cleaned = cleaned[:max_length].rstrip("-.")
    return cleaned


def safe_artifact_leaf(name: str, version: str | None, artifact_id: str) -> str:
    """Deterministic, readable, stable filesystem leaf for one artifact.

    A non-empty version joins as ``f"{sanitize(name)}--{sanitize(version)}"``
    (e.g. "IVMSD--v1"); a None or empty version yields just the sanitized
    name. ``artifact_id`` takes no part in the leaf itself: same-batch leaf
    collisions are resolved afterwards with :func:`dedupe_leaves`.
    """
    base = sanitize_component(name)
    if version is not None and version != "":
        return f"{base}--{sanitize_component(version)}"
    return base


def normalize_remote_path(path: str) -> str:
    """Normalize a remote path: posixpath.normpath, then drop trailing "/".

    The root "/" stays "/"; empty or whitespace-only input raises
    ConflictError because a remote path must point somewhere. Relative paths
    are normalized but not rejected. posixpath keeps exactly two leading
    slashes ("//data") per POSIX; :func:`paths_overlap` compares components,
    so that quirk does not affect ancestor checks.
    """
    candidate = path.strip()
    if not candidate:
        raise ConflictError("remote path is empty")
    normalized = posixpath.normpath(candidate)
    if len(normalized) > 1:
        normalized = normalized.rstrip("/") or "/"
    return normalized


def paths_overlap(a: str, b: str) -> bool:
    """True when both normalized paths are equal or ancestor/descendant.

    Component-wise comparison, never a string prefix check: "/data/x"
    overlaps "/data/x/cache" but not "/data/xy". The root "/" is an ancestor
    of every path. An empty side raises ConflictError via normalization.
    """
    parts_a = _components(a)
    parts_b = _components(b)
    if len(parts_a) > len(parts_b):
        parts_a, parts_b = parts_b, parts_a
    return parts_b[: len(parts_a)] == parts_a


def _components(path: str) -> list[str]:
    normalized = normalize_remote_path(path)
    return [part for part in normalized.split("/") if part]


def dedupe_leaves(leaves: dict[str, str], artifact_ids: dict[str, str]) -> dict[str, str]:
    """Resolve leaf collisions inside one batch, deterministically.

    ``leaves`` maps artifact_id -> sanitized leaf (as produced by
    :func:`safe_artifact_leaf`) in batch order; ``artifact_ids`` maps
    artifact_id -> artifact_id for prefix extraction. The first occurrence of
    a leaf keeps it; each later duplicate gets "--" plus a prefix of its own
    artifact_id appended (6, 8, 12 characters, then the full id). Iteration
    follows the caller's insertion order and the seen-set is used for
    membership only, so repeated calls with the same input return the same
    mapping. Raises ConflictError if even the full-id suffix is taken.
    """
    resolved: dict[str, str] = {}
    taken: set[str] = set()
    for artifact_id, leaf in leaves.items():
        if leaf not in taken:
            resolved[artifact_id] = leaf
            taken.add(leaf)
            continue
        owner_id = artifact_ids.get(artifact_id, artifact_id)
        for width in (6, 8, 12, len(owner_id)):
            candidate = f"{leaf}--{owner_id[:width]}"
            if candidate not in taken:
                resolved[artifact_id] = candidate
                taken.add(candidate)
                break
        else:
            raise ConflictError(f"artifact leaf for {artifact_id} cannot be deduplicated")
    return resolved

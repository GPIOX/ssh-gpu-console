"""Probe-failure reason taxonomy (single source).

The ssh stderr → ``DirectAuthReason`` classifier shared by the direct-auth
checks (service) and the transfer planner preflight; both consume this module
so a failure string is classified identically everywhere.
"""

from __future__ import annotations


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

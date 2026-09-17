"""Single shared WebSocket endpoint for fleet and selected-server telemetry.

This module owns the origin check only; the session protocol (hello frame,
4 KiB cap, select-only intent parsing, cleanup) lives in TelemetryService.
"""

from __future__ import annotations

import contextlib
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.runtime import get_runtime

router = APIRouter(tags=["realtime"])

_LOCAL_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if origin is None:
        # Native clients and local test calls may omit Origin entirely.
        return True
    try:
        parsed = urlsplit(origin)
        hostname = parsed.hostname
        _ = parsed.port  # validates malformed port syntax without trusting it
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.username is None
        and parsed.password is None
        and hostname is not None
        and hostname.casefold() in _LOCAL_ORIGIN_HOSTS
    )


@router.websocket("/api/v1/realtime")
async def realtime(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket):
        await websocket.close(code=1008, reason="origin not allowed")
        return
    # Normal client hangup; the service already ran cleanup in its finally.
    with contextlib.suppress(WebSocketDisconnect):
        await get_runtime().telemetry.handle_websocket(websocket)

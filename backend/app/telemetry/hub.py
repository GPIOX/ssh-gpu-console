"""Shared realtime fan-out with bounded latest-state queues.

Newest-wins per client: every client owns a bounded queue (drop-oldest) and
a private sender task, so one slow websocket never blocks broadcast or other
clients. Fleet frames are revision-gated — unchanged data is never resent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.models.server import ServerRecord
from app.telemetry.state import TelemetryState


class RealtimeConnection(Protocol):
    """The slice of a websocket the hub needs (FastAPI-compatible)."""

    async def accept(self) -> None: ...

    async def send_text(self, message: str) -> None: ...

    async def receive_text(self) -> str: ...


@dataclass(eq=False)
class _Client:
    websocket: RealtimeConnection
    queue_size: int
    queue: asyncio.Queue[dict[str, Any]] = field(init=False)
    sender: asyncio.Task[None] | None = None
    fleet_revision: int = -1
    snapshot_version: int = -1

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=max(1, self.queue_size))

    def enqueue(self, message: dict[str, Any]) -> None:
        """Newest-wins: when full, drop the oldest pending frame."""

        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(message)


class RealtimeHub:
    """Fan-out of fleet, selected-server and status frames to all clients."""

    def __init__(
        self,
        settings: object,
        state: TelemetryState,
        records_provider: Callable[[], Sequence[ServerRecord]],
    ) -> None:
        self._settings = settings
        self._state = state
        self._records_provider = records_provider
        self._clients: set[_Client] = set()
        self._selected: str | None = None
        self._wake = asyncio.Event()
        self._broadcast_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._sent_status: dict[str, str] = {}

    # ---- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._broadcast_task is None or self._broadcast_task.done():
            self._stopping = False
            self._broadcast_task = asyncio.create_task(
                self._broadcast_loop(), name="telemetry-realtime-broadcast"
            )

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._broadcast_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._broadcast_task = None
        for client in list(self._clients):
            self._cancel_sender(client)
        self._clients.clear()

    def wake(self) -> None:
        """Nudge the broadcast loop (used as the state change listener)."""

        self._wake.set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def selected(self) -> str | None:
        return self._selected

    # ---- client lifecycle ---------------------------------------------------

    async def connect(self, websocket: RealtimeConnection) -> _Client:
        await websocket.accept()
        client = _Client(
            websocket=websocket,
            queue_size=int(getattr(self._settings, "client_queue_size", 2)),
        )
        self._clients.add(client)
        client.sender = asyncio.create_task(
            self._send_loop(client), name="telemetry-realtime-client"
        )
        # A first frame makes the connection useful before the next interval.
        client.enqueue(
            {
                "type": "hello",
                "server_ids": [record.server_id for record in self._records_provider()],
            }
        )
        self._wake.set()
        return client

    async def disconnect(self, client: _Client) -> None:
        if client not in self._clients:
            return
        self._clients.discard(client)
        self._cancel_sender(client)

    def _cancel_sender(self, client: _Client) -> None:
        if client.sender is not None and client.sender is not asyncio.current_task():
            client.sender.cancel()
            client.sender = None

    def set_intent(self, server_id: str | None) -> None:
        """Accept only selection intent; resets per-client revisions so the
        fleet and selected-server frames are re-sent immediately."""

        if server_id is None:
            self._selected = None
        else:
            if not isinstance(server_id, str):
                raise ValueError("server_id must be a string or null")
            candidate = server_id.strip()[:128]
            if not candidate:
                raise ValueError("server_id must not be empty")
            self._selected = candidate
        for client in self._clients:
            client.fleet_revision = -1
            client.snapshot_version = -1
        self._wake.set()

    # ---- broadcast ----------------------------------------------------------

    async def _broadcast_loop(self) -> None:
        try:
            while not self._stopping:
                # Clear before checking: a mutation racing this boundary sets
                # the event again and re-wakes the loop immediately.
                self._wake.clear()
                self._push_changes()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
        except asyncio.CancelledError:
            raise

    def _push_changes(self) -> None:
        state = self._state
        fleet_revision = state.fleet_revision()
        fleet_frame: dict[str, Any] | None = None
        for client in list(self._clients):
            if client.fleet_revision == fleet_revision:
                continue
            if fleet_frame is None:
                fleet_frame = {
                    "type": "fleet",
                    "summary": state.fleet_summary(self._records_provider()).model_dump(
                        mode="json"
                    ),
                }
            client.enqueue(fleet_frame)
            client.fleet_revision = fleet_revision

        selected = self._selected
        if selected is not None and state.exists(selected):
            version = state.version_of(selected)
            snapshot_frame: dict[str, Any] | None = None
            for client in list(self._clients):
                if client.snapshot_version == version:
                    continue
                if snapshot_frame is None:
                    snapshot = state.snapshot(selected)
                    if snapshot is None:
                        break
                    snapshot_frame = {
                        "type": "server",
                        "server_id": selected,
                        "snapshot": snapshot.model_dump(mode="json"),
                    }
                client.enqueue(snapshot_frame)
                client.snapshot_version = version

        for server_id in state.server_ids():
            status = state.status_of(server_id).value
            if self._sent_status.get(server_id) == status:
                continue
            self._sent_status[server_id] = status
            if status == "unknown":
                continue  # a freshly-created/never-collected server is not news
            frame = {"type": "status", "server_id": server_id, "status": status}
            for client in list(self._clients):
                client.enqueue(frame)

    async def _send_loop(self, client: _Client) -> None:
        try:
            while True:
                message = await client.queue.get()
                await client.websocket.send_text(json.dumps(message, default=str))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Broken websocket: stop serving this client; its handler will
            # observe the disconnect and run cleanup.
            self._clients.discard(client)

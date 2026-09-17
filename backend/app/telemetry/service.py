"""TelemetryService facade: the single object the API and main.py touch.

Owns the shared state, the demand-aware scheduler and the realtime hub.
``executor_factory`` is dependency-injected so tests can supply scripted
fakes; production wires ``build_executor`` over the SSH manager.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Protocol

from app.models.server import ServerRecord
from app.models.telemetry import (
    FleetSummary,
    HistoryPoint,
    ServerSnapshot,
    ServerStatus,
)
from app.telemetry.history import GPU_METRICS
from app.telemetry.hub import RealtimeConnection, RealtimeHub
from app.telemetry.scheduler import ExecutorFactory, Scheduler
from app.telemetry.state import TelemetryState

_MAX_MESSAGE_BYTES = 4096


class RegistryLike(Protocol):
    """Structural view of the server registry the service needs."""

    def all(self) -> Sequence[ServerRecord]: ...


class TelemetryService:
    """Public telemetry interface used by REST routes, the WebSocket route
    and the composition root."""

    def __init__(
        self,
        settings: object,
        registry: RegistryLike | None,
        executor_factory: ExecutorFactory,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._state = TelemetryState(settings)
        self._hub = RealtimeHub(settings, self._state, self._records)
        self._scheduler = Scheduler(settings, self._state, executor_factory)
        self._clients = 0
        self._selected: str | None = None

    # ---- lifecycle ----------------------------------------------------------

    def _records(self) -> list[ServerRecord]:
        if self._registry is None:
            return []
        try:
            return list(self._registry.all())
        except Exception:  # a broken registry must not kill telemetry
            return []

    async def start(self) -> None:
        self._state.set_listener(self._hub.wake)
        await self._scheduler.start()
        await self._hub.start()
        self.reconcile_servers(self._records())

    async def stop(self) -> None:
        await self._scheduler.stop()
        await self._hub.stop()
        self._state.set_listener(None)

    # ---- reads --------------------------------------------------------------

    def snapshot(self, server_id: str) -> ServerSnapshot | None:
        if not self.has_server(server_id):
            return None
        return self._state.snapshot(server_id)

    def fleet_summary(self) -> FleetSummary:
        return self._state.fleet_summary(self._records())

    def gpu_history(self, server_id: str, gpu_index: int, metric: str) -> list[HistoryPoint]:
        if metric not in GPU_METRICS:
            raise ValueError(f"unknown gpu history metric: {metric!r}")
        if gpu_index < 0:
            raise ValueError("gpu index must be non-negative")
        return self._state.gpu_history(server_id, gpu_index, metric)

    def status_of(self, server_id: str) -> ServerStatus:
        return self._state.status_of(server_id)

    def has_server(self, server_id: str) -> bool:
        if any(record.server_id == server_id for record in self._records()):
            return True
        return self._state.exists(server_id)

    # ---- demand -------------------------------------------------------------

    def set_selected(self, server_id: str | None) -> None:
        if server_id is not None:
            if not isinstance(server_id, str):
                raise ValueError("server_id must be a string or null")
            candidate = server_id.strip()[:128]
            if not candidate:
                raise ValueError("server_id must not be empty")
            server_id = candidate
        self._selected = server_id
        self._hub.set_intent(server_id)
        self._apply_demand()

    def note_client_connected(self) -> None:
        self._clients += 1
        self._apply_demand()

    def note_client_disconnected(self) -> None:
        self._clients = max(0, self._clients - 1)
        if self._clients == 0:
            # The last browser leaving clears the selection; the scheduler's
            # grace period keeps collection alive briefly to absorb flaps.
            self.set_selected(None)
        self._apply_demand()

    def _apply_demand(self) -> None:
        selected = {self._selected} if self._selected else set()
        self._scheduler.set_demand(self._clients, selected)

    # ---- reconciliation -----------------------------------------------------

    def reconcile_servers(self, records: Sequence[ServerRecord]) -> None:
        self._scheduler.reconcile(list(records))
        enabled_ids = {record.server_id for record in records if record.enabled}
        if self._selected is not None and self._selected not in enabled_ids:
            self.set_selected(None)

    # ---- realtime -----------------------------------------------------------

    async def handle_websocket(self, websocket: RealtimeConnection) -> None:
        """One browser realtime session: hello, selection intent frames, and
        guaranteed cleanup. Frames larger than 4 KiB and anything that is not
        a valid select intent are ignored."""

        client = await self._hub.connect(websocket)
        self.note_client_connected()
        try:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > _MAX_MESSAGE_BYTES:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "select":
                    continue
                server_id = message.get("server_id")
                if server_id is not None and not isinstance(server_id, str):
                    continue
                try:
                    self.set_selected(server_id)
                except ValueError:
                    continue
        finally:
            await self._hub.disconnect(client)
            self.note_client_disconnected()

    @property
    def client_count(self) -> int:
        return self._hub.client_count

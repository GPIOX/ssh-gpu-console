"""One demand-aware scheduler shared by all realtime clients.

Design (ported from v2, audit 5/5):
- at most one worker task per enabled server and none per browser client;
- tiers: FAST (cpu_memory + gpu), MEDIUM (gpu_processes + network),
  PROCESS (ps), SLOW (storage), STATIC (system);
- selected servers run every tier at the interactive cadence; non-selected
  servers only run FAST at the fleet cadence (15 s active / 45 s idle);
- idle mode (no realtime clients, after a grace period) suspends the
  selected-only MEDIUM/PROCESS/SLOW tiers;
- demand changes wake workers through per-worker events and a revision
  counter (clear-then-check, no polling);
- one collector's failure never destroys unrelated sections; transport
  failures map to a server status and an exponential backoff instead of
  hammering the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Protocol

from app.collectors.base import SECTION_GPU_PROCESSES, CollectorError, ExecutorLike
from app.collectors.cpu import CpuCollector
from app.collectors.gpu import FleetGpuProcCollector, GpuCollector, GpuProcessCollector
from app.collectors.memory import MemoryCollector
from app.collectors.network import NetworkCollector
from app.collectors.processes import ProcessCollector
from app.collectors.storage import StorageCollector
from app.collectors.system_info import SystemInfoCollector
from app.core.logging import get_logger
from app.models.server import ServerRecord
from app.models.telemetry import (
    CpuInfo,
    FleetProcsSample,
    GpuInfo,
    GpuProcessInfo,
    MemoryInfo,
    NetworkInterfaceInfo,
    ProcessInfo,
    ServerStatus,
    StorageMount,
    SystemInfo,
)
from app.ssh.commands import CPU_MEMORY, CommandSpec
from app.ssh.executor import Executor, ExecutorError, RemoteCommandResult
from app.telemetry.state import TelemetryState

FAST = "fast"
MEDIUM = "medium"
PROCESS = "process"
SLOW = "slow"
STATIC = "static"
FLEETPROCS = "fleetprocs"
GROUPS = (FLEETPROCS, STATIC, FAST, MEDIUM, PROCESS, SLOW)

logger = get_logger("telemetry.scheduler")

# ExecutorError code -> server status (mirrors the SSH layer's taxonomy).
_STATUS_BY_TRANSPORT_CODE: dict[str, ServerStatus] = {
    "timeout": ServerStatus.TIMEOUT,
    "authentication_failed": ServerStatus.AUTHENTICATION_FAILED,
    "host_key_mismatch": ServerStatus.HOST_KEY_ERROR,
    "host_key_unknown": ServerStatus.HOST_KEY_ERROR,
    "connect_failed": ServerStatus.OFFLINE,
    "connection_lost": ServerStatus.OFFLINE,
    "reconnect_backoff": ServerStatus.RECONNECTING,
    "cancelled": ServerStatus.OFFLINE,
    "unknown": ServerStatus.OFFLINE,
}


class ExecutorFactory(Protocol):
    async def __call__(self, server_id: str, record: ServerRecord) -> Executor: ...


class SectionCollector(Protocol):
    """Collector boundary used by the scheduler."""

    name: str

    @property
    def spec(self) -> CommandSpec: ...

    async def collect(
        self, executor: ExecutorLike, result: RemoteCommandResult | None = None
    ) -> object: ...


def _identity(record: ServerRecord) -> tuple[str, str | None, int | None]:
    return (record.ssh_host, record.username, record.port)


class Scheduler:
    """Central collection pipeline; owns all telemetry timing."""

    def __init__(
        self, settings: object, state: TelemetryState, executor_factory: ExecutorFactory
    ) -> None:
        self._settings = settings
        self._state = state
        self._executor_factory = executor_factory
        self._records: dict[str, ServerRecord] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._collectors: dict[str, dict[str, SectionCollector]] = {}
        self._detail_servers: set[str] = set()
        self._active_clients = 0
        self._last_client_seen = 0.0
        self._demand_revision = 0
        self._wakeups: dict[str, asyncio.Event] = {}
        self._stop_event = asyncio.Event()
        self._started = False

    # ---- demand / lifecycle -------------------------------------------------

    def set_demand(self, active_clients: int, detail_servers: Iterable[str]) -> None:
        next_clients = max(0, int(active_clients))
        next_detail = set(detail_servers)
        changed = next_clients != self._active_clients or next_detail != self._detail_servers
        self._active_clients = next_clients
        if self._active_clients:
            self._last_client_seen = time.monotonic()
        self._detail_servers = next_detail
        if changed:
            self._demand_revision += 1
            for wakeup in tuple(self._wakeups.values()):
                wakeup.set()

    def mode(self) -> str:
        if self._active_clients > 0:
            return "interactive"
        grace = float(getattr(self._settings, "idle_grace_s", 20.0))
        if self._last_client_seen and time.monotonic() - self._last_client_seen < grace:
            return "interactive"
        return "idle"

    def detail_servers(self) -> set[str]:
        return set(self._detail_servers)

    def managed_ids(self) -> set[str]:
        return set(self._records)

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()

    async def stop(self) -> None:
        self._stop_event.set()
        for wakeup in tuple(self._wakeups.values()):
            wakeup.set()
        tasks = list(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        self._records.clear()
        self._collectors.clear()
        self._started = False

    # ---- reconciliation -----------------------------------------------------

    def reconcile(self, records: Sequence[ServerRecord]) -> None:
        """Apply registry changes: start/stop workers, reset on identity change.

        Called explicitly on the notification path (TelemetryService), so no
        polling loop is needed.
        """

        enabled = {record.server_id: record for record in records if record.enabled}
        for server_id, record in enabled.items():
            previous = self._records.get(server_id)
            task = self._workers.get(server_id)
            sampling_changed = (
                previous is not None and previous.sample_interval_s != record.sample_interval_s
            )
            if previous is not None and sampling_changed and task is not None and not task.done():
                # The worker captured the record at start; restart to pick up
                # the new cadence (connection identity is untouched).
                task.cancel()
                self._workers.pop(server_id, None)
                self._collectors.pop(server_id, None)
                task = None
            if previous is not None and _identity(previous) != _identity(record):
                # A reused id now points at a different host: never expose the
                # old host's telemetry or network counter deltas.
                if task is not None and not task.done():
                    task.cancel()
                self._workers.pop(server_id, None)
                self._collectors.pop(server_id, None)
                self._state.clear(server_id)
                task = None
            if task is None or task.done():
                self._workers[server_id] = asyncio.create_task(
                    self._worker(record), name=f"telemetry-{server_id}"
                )
            self._records[server_id] = record
        for server_id in list(self._workers):
            if server_id not in enabled:
                task = self._workers.pop(server_id)
                task.cancel()
                self._collectors.pop(server_id, None)
                self._records.pop(server_id, None)
        self._state.prune(enabled)

    # ---- timing -------------------------------------------------------------

    def _interval(
        self,
        group: str,
        interactive: bool,
        detail: bool,
        record: ServerRecord | None = None,
    ) -> float:
        s = self._settings
        if group == FLEETPROCS:
            # Always at fleet cadence — the owning-user probe is not detail data.
            if interactive:
                return max(0.05, float(getattr(s, "fleet_interval_active", 15.0)))
            return max(0.05, float(getattr(s, "fleet_interval_idle", 45.0)))
        if group == STATIC:
            return max(0.05, float(getattr(s, "static_interval", 600.0)))
        if group == MEDIUM:
            return max(0.05, float(getattr(s, "medium_interval", 5.0)))
        if group == PROCESS:
            return max(0.05, float(getattr(s, "process_interval", 7.5)))
        if group == SLOW:
            return max(0.05, float(getattr(s, "slow_interval", 30.0)))
        # FAST: interactive detail runs hot; everyone else runs at fleet cadence.
        if detail and interactive:
            override = getattr(record, "sample_interval_s", None)
            if override is not None:
                # Per-server detail cadence, bounded for SSH load safety.
                return max(0.5, min(600.0, float(override)))
            return max(0.05, float(getattr(s, "fast_interval", 2.5)))
        if interactive:
            return max(0.05, float(getattr(s, "fleet_interval_active", 15.0)))
        return max(0.05, float(getattr(s, "fleet_interval_idle", 45.0)))

    def _enabled(self, group: str, *, detail: bool, interactive: bool) -> bool:
        if group == FAST:
            return True
        if group == FLEETPROCS:
            return True
        if group == STATIC:
            # One command per 10 min per server: keeps FleetEntry.os_pretty
            # fresh for servers nobody has selected yet.
            return True
        return detail and interactive  # MEDIUM, PROCESS, SLOW

    def _backoff_delay(self, failures: int) -> float:
        base = float(getattr(self._settings, "reconnect_backoff_min_s", 2.0))
        maximum = float(getattr(self._settings, "reconnect_backoff_max_s", 60.0))
        delay = min(maximum, base * (2 ** max(0, failures - 1)))
        return delay + random.uniform(0.0, 0.5)

    # ---- worker -------------------------------------------------------------

    async def _worker(self, record: ServerRecord) -> None:
        server_id = record.server_id
        wakeup = asyncio.Event()
        self._wakeups[server_id] = wakeup
        observed_demand = -1
        next_due: dict[str, float] = dict.fromkeys(GROUPS, 0.0)
        consecutive_failures = 0
        executor: Executor | None = None
        try:
            while not self._stop_event.is_set():
                interactive = self.mode() == "interactive"
                detail = server_id in self._detail_servers
                if observed_demand != self._demand_revision:
                    # Demand transitions must not wait out the idle cadence.
                    next_due[FAST] = 0.0
                    if interactive and detail:
                        next_due[MEDIUM] = 0.0
                        next_due[PROCESS] = 0.0
                    observed_demand = self._demand_revision
                now = time.monotonic()
                due = [
                    group
                    for group in GROUPS
                    if self._enabled(group, detail=detail, interactive=interactive)
                    and next_due[group] <= now
                ]
                if due and executor is None:
                    try:
                        executor = await self._executor_factory(server_id, record)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        consecutive_failures += 1
                        retry_at = time.monotonic() + self._backoff_delay(consecutive_failures)
                        self._mark_transport_failure(server_id, "connect_failed", str(exc))
                        for group in GROUPS:
                            next_due[group] = retry_at
                        due = []
                if due and executor is not None:
                    code = await self._collect(server_id, executor, due)
                    if code is not None:
                        consecutive_failures += 1
                        retry_at = time.monotonic() + self._backoff_delay(consecutive_failures)
                    else:
                        consecutive_failures = 0
                        retry_at = 0.0
                    finished = time.monotonic()
                    for group in due:
                        next_due[group] = finished + self._interval(
                            group, interactive, detail, record
                        )
                    if retry_at:
                        for group in GROUPS:
                            next_due[group] = max(next_due[group], retry_at)
                else:
                    for group in GROUPS:
                        if not self._enabled(group, detail=detail, interactive=interactive):
                            next_due[group] = time.monotonic()
                enabled_times = [
                    next_due[group]
                    for group in GROUPS
                    if self._enabled(group, detail=detail, interactive=interactive)
                ]
                current = time.monotonic()
                delay = max(0.05, min(60.0, min(enabled_times, default=current + 60.0) - current))
                # Clear before checking the revision so a demand change racing
                # this boundary cannot be lost: a change after the check sets
                # the event and wakes this worker immediately.
                wakeup.clear()
                if observed_demand != self._demand_revision:
                    continue
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wakeup.wait(), timeout=delay)
        except asyncio.CancelledError:
            raise
        finally:
            if self._wakeups.get(server_id) is wakeup:
                self._wakeups.pop(server_id, None)
            if executor is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await executor.close()

    # ---- collection ---------------------------------------------------------

    def _collector(
        self, server_id: str, key: str, factory: Callable[[], SectionCollector]
    ) -> SectionCollector:
        cached = self._collectors.setdefault(server_id, {})
        item = cached.get(key)
        if item is None:
            item = factory()
            cached[key] = item
        return item

    def _plan(
        self, server_id: str, groups: Sequence[str]
    ) -> list[tuple[SectionCollector, CommandSpec]]:
        plan: list[tuple[SectionCollector, CommandSpec]] = []
        gpu_unavailable = self._state.errors_of(server_id).get("gpu") == "unavailable"
        for group in groups:
            if group == STATIC:
                system = self._collector(server_id, "system", SystemInfoCollector)
                plan.append((system, system.spec))
            elif group == FAST:
                gpu = self._collector(server_id, "gpu", GpuCollector)
                cpu = self._collector(server_id, "cpu", CpuCollector)
                memory = self._collector(server_id, "memory", MemoryCollector)
                plan.extend([(gpu, gpu.spec), (cpu, CPU_MEMORY), (memory, CPU_MEMORY)])
            elif group == FLEETPROCS:
                fleet_procs = self._collector(server_id, "fleet_procs", FleetGpuProcCollector)
                plan.append((fleet_procs, fleet_procs.spec))
            elif group == MEDIUM:
                network = self._collector(server_id, "network", NetworkCollector)
                plan.append((network, network.spec))
                if not gpu_unavailable:
                    gpu_processes = self._collector(server_id, "gpu_processes", GpuProcessCollector)
                    plan.append((gpu_processes, gpu_processes.spec))
            elif group == PROCESS:
                processes = self._collector(server_id, "processes", ProcessCollector)
                plan.append((processes, processes.spec))
            elif group == SLOW:
                storage = self._collector(server_id, "storage", StorageCollector)
                plan.append((storage, storage.spec))
        return plan

    async def _collect(
        self,
        server_id: str,
        executor: Executor,
        groups: Sequence[str],
    ) -> str | None:
        """Run one batch; returns the transport failure code if one occurred."""

        plan = self._plan(server_id, groups)
        if not plan:
            return None
        specs: list[CommandSpec] = []
        seen: set[str] = set()
        for _collector, spec in plan:
            if spec.key not in seen:
                seen.add(spec.key)
                specs.append(spec)
        outcomes = await asyncio.gather(
            *(executor.run(spec.command, timeout_s=spec.timeout_s) for spec in specs),
            return_exceptions=True,
        )
        result_by_key = {spec.key: outcome for spec, outcome in zip(specs, outcomes, strict=True)}
        transport_code: str | None = None
        transport_detail: str | None = None
        for collector, spec in plan:
            outcome: object = result_by_key.get(spec.key)
            if isinstance(outcome, BaseException):
                if isinstance(outcome, ExecutorError):
                    code, message = outcome.code, outcome.detail
                    if transport_code is None:
                        transport_code = code
                        transport_detail = message
                else:
                    code, message = "unknown", str(outcome)
                self._state.set_error(server_id, collector.name, code)
                logger.debug("command %s failed for %s: %s", spec.key, server_id, message)
                continue
            try:
                data = await collector.collect(
                    executor,
                    outcome,  # type: ignore[arg-type]
                )
            except CollectorError as exc:
                self._state.set_error(server_id, collector.name, exc.code)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # parser bugs must stay isolated
                logger.debug("collector %s failed for %s: %s", collector.name, server_id, exc)
                self._state.set_error(server_id, collector.name, "unknown")
                continue
            self._store(server_id, collector.name, data)
            self._state.clear_error(server_id, collector.name)
        if transport_code is not None:
            self._mark_transport_failure(server_id, transport_code, transport_detail or "")
            return transport_code
        self._sync_status(server_id)
        return None

    def _store(self, server_id: str, name: str, data: object) -> None:
        if name == SECTION_GPU_PROCESSES and isinstance(data, FleetProcsSample):
            self._state.update_pid_users(server_id, data.pid_users)
            self._state.update_gpu_processes(server_id, data.gpu_processes)
        elif name == "gpu" and isinstance(data, list):
            self._state.update_gpus(server_id, [item for item in data if isinstance(item, GpuInfo)])
        elif name == "gpu_processes" and isinstance(data, list):
            self._state.update_gpu_processes(
                server_id, [item for item in data if isinstance(item, GpuProcessInfo)]
            )
        elif name == "cpu" and isinstance(data, CpuInfo):
            self._state.update_cpu(server_id, data)
        elif name == "memory" and isinstance(data, MemoryInfo):
            self._state.update_memory(server_id, data)
        elif name == "storage" and isinstance(data, list):
            self._state.update_storage(
                server_id, [item for item in data if isinstance(item, StorageMount)]
            )
        elif name == "network" and isinstance(data, list):
            self._state.update_network(
                server_id, [item for item in data if isinstance(item, NetworkInterfaceInfo)]
            )
        elif name == "processes" and isinstance(data, list):
            self._state.update_processes(
                server_id, [item for item in data if isinstance(item, ProcessInfo)]
            )
        elif name == "system" and isinstance(data, SystemInfo):
            self._state.update_system(server_id, data)

    # ---- status -------------------------------------------------------------

    def _mark_transport_failure(self, server_id: str, code: str, detail: str) -> None:
        status = _STATUS_BY_TRANSPORT_CODE.get(code, ServerStatus.OFFLINE)
        self._state.set_status(server_id, status, detail[:300])

    def _sync_status(self, server_id: str) -> None:
        errors = self._state.errors_of(server_id)
        # A missing NVIDIA stack is a capability fact, not degradation.
        blocking = {section: code for section, code in errors.items() if code != "unavailable"}
        if blocking:
            detail = "; ".join(f"{section}: {code}" for section, code in sorted(blocking.items()))
            self._state.set_status(server_id, ServerStatus.DEGRADED, detail[:300])
        else:
            self._state.set_status(server_id, ServerStatus.ONLINE, "")

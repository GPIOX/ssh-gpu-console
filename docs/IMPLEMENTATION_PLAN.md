# V3 Implementation Plan — Contracts & Task Graph

Architecture ownership: the maintainers own module boundaries, API contracts, shared models,
sampling policy, design system, integration, and acceptance. Changes outside an assigned
scope require review instead of unilateral contract changes.

## System shape

```text
React/Vite (pnpm)  ── REST + one WebSocket ──▶  FastAPI modular monolith
                                                  │ registry │ scheduler │ realtime hub │ actions
                                                  │ executor protocol (only app/ssh imports asyncssh)
                                                  ▼
                                        AsyncSSH managed connections ──▶ remote Linux servers
```

- telemetry path: `SSH → typed parser → bounded RAM → shared WebSocket → bounded browser store`;
- registry path: explicit CRUD → atomic JSON replace under `backend/data/registry.json`;
- production: FastAPI serves `frontend/dist`; dev: `pnpm dev` proxies `/api` to :8420.

## API contract (`/api/v1`, all JSON, Pydantic `extra="forbid"`)

REST:

| Method & path | Purpose |
|---|---|
| `GET /servers` | registry list |
| `POST /servers` | create (body: ServerCreate) |
| `PATCH /servers/{server_id}` | update (body: ServerPatch) |
| `DELETE /servers/{server_id}` | remove |
| `GET /servers/ssh-config/aliases` | parsed `~/.ssh/config` aliases (alias, host, user?, port?) |
| `POST /servers/{server_id}/test` | connection test → `ConnectionTestResult` (may carry `pending_host_key`) |
| `POST /servers/{server_id}/host-key/trust` | explicit TOFU consent; persists to app-owned trust file, never touches `~/.ssh/known_hosts` |
| `GET /telemetry/fleet` | `FleetSummary` (per-server low-cost entries) |
| `GET /telemetry/servers/{server_id}` | `ServerSnapshot` (full detail, per-section freshness/errors) |
| `GET /telemetry/servers/{server_id}/gpu-history?gpu=0&metric=utilization` | bounded history points |
| `POST /servers/{server_id}/actions/terminate-process` | body `{pid:int}` — SIGTERM |
| `POST /servers/{server_id}/actions/kill-process` | body `{pid:int}` — SIGKILL |

Status taxonomy: `unknown, connecting, online, reconnecting, offline, timeout,
authentication_failed, host_key_error, degraded`. No endpoint accepts shell strings.

WebSocket `/api/v1/realtime`:

- client→server: `{"type":"select","server_id":str|null}`, optional `{"type":"ping"}`;
- server→client: `{"type":"hello","server_ids":[…]}`, `{"type":"fleet","summary":FleetSummary}`,
  `{"type":"server","server_id":…,"snapshot":ServerSnapshot}`,
  `{"type":"status","server_id":…,"status":…}`;
- per-client outbound queue: latest-state, max 2 pending frames (drop-oldest);
- interactive mode = ≥1 realtime client; idle grace 20 s after last disconnect.

## Composition

`app/runtime.py` holds a typed `Runtime(settings, ssh, telemetry)` container set by `main.py`;
routes resolve it via `get_runtime()`. `app/telemetry/service.py` exposes the `TelemetryService`
facade (`start/stop, snapshot, fleet_summary, gpu_history, set_selected, note_client_connected/
disconnected`) that owns state + scheduler + hub internally.

## Shared models (in `backend/app/models/`)

`server.py`: `ServerRecord, ServerCreate, ServerPatch, AliasEntry, ConnectionTestResult`.
`telemetry.py`: `ServerStatus, GpuAvailability, CpuInfo, MemoryInfo, GpuInfo, GpuProcessInfo,
ProcessInfo, StorageMount, NetworkInterfaceInfo, SystemInfo, SectionState, ServerSnapshot,
FleetEntry, FleetSummary, HistoryPoint`. Field semantics: bytes are ints (suffix `_b`),
percent floats 0–100, `None` = not reported (render `N/A`), never raise on unsupported fields.

## Remote command specs (single source: `app/ssh/commands.py`)

| Collector | One command (fixed, read-only) |
|---|---|
| gpu | keys `gpu`/`gpu_basic` (driver fallback): `nvidia-smi --query-gpu=index,uuid,name,driver_version,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed --format=csv,noheader,nounits` (one round trip; missing binary → availability=unavailable) |
| gpu_processes | `nvidia-smi --query-compute-apps=pid,process_name,used_memory,gpu_uuid --format=csv,noheader,nounits` |
| cpu+memory | `cat /proc/stat /proc/loadavg /proc/meminfo` (CPU % = delta between consecutive samples, computed in scheduler state) |
| storage | `df -kP -x tmpfs -x devtmpfs -x efivarfs -x squashfs -x overlay` |
| network | `cat /proc/net/dev` (rates from counter deltas in state) |
| processes | `ps -eo pid,user:24,pcpu,pmem,rss,stat,comm,args --sort=-pcpu` (truncate client-side) |
| system | `hostname; uname -r; grep PRETTY_NAME /etc/os-release; cat /proc/uptime; nvidia-smi --version` guarded |

GPU process ↔ process correlation: join on PID from the processes sample (or on demand).

## Sampling contract

- selected server (interactive): fast cpu/mem/gpu 2.5 s · gpu processes + network 5 s ·
  processes 7.5 s · storage 30 s · static system 10 min;
- fleet tier (all enabled servers): 15 s interactive, 45 s idle;
- idle mode (no realtime clients, 20 s grace): suspend selected-only collectors;
- reconnect backoff: bounded exponential 2–60 s + jitter; first connect is lazy + staggered;
- GPU history: deque(maxlen=300) per metric per GPU; fleet carries no history.

## Task graph & scopes

| # | Task | Scope (exclusive) | Depends on |
|---|---|---|---|
| L1 | SSH infrastructure + registry + servers API | `app/ssh/*` (impl), `app/servers/*`, `app/api/v1/servers.py`, `app/core/{config,logging,errors}.py`, tests | contracts (done by Sol) |
| L2 | Collectors + telemetry state/scheduler/hub + telemetry API | `app/collectors/*`, `app/telemetry/*`, `app/api/v1/{telemetry,realtime}.py`, tests | contracts |
| L3 | Actions + app assembly + static serving | `app/actions/*`, `app/api/v1/actions.py`, `app/main.py`, `app/core/lifecycle.py`, tests | L1+L2 |
| F1 | Frontend foundation: tokens, primitives, clients, store, shell | `frontend/*` scaffold, `src/{styles,design,gpu,services,store,types,utils}/*` | API contract |
| F2 | Fleet UI | `src/features/fleet/*` | F1 |
| F3 | Server detail UI (Overview/GPUs/Processes/System/Storage/Network) | `src/features/server/*` | F1 |
| F4 | Onboarding + actions UI + settings | `src/features/onboarding/*`, dialogs | F1 (F2 for menu hooks) |

Agent boundaries: never touch another task's scope, contracts, or docs. Report contract issues;
Sol decides. Backend tasks L1/L2 run in parallel against the fixed contracts; L3 integrates.

## Definition of MVP complete

Runs with documented commands; onboarding with explicit SSH errors; Fleet + 6 detail sections;
multi-GPU/no-GPU/offline/stale/partial handled; terminate/kill validated named actions;
SSH reuse + bounded queues + backpressure tested; lint/type/tests/build pass; rendered UI
visually accepted by Sol; resource + disk-write audits measured.

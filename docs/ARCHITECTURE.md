# ARCHITECTURE.md

## 1. Architecture Goal

Build a lightweight local control plane for agentless management of remote Linux/GPU servers over SSH.

Optimize for:

- low local overhead;
- low remote overhead;
- bounded RAM;
- minimal disk writes;
- SSH reuse;
- multi-server isolation;
- maintainability;
- security.

---

## 2. Deployment Topology

```text
┌─────────────────────────────────────────────┐
│              Local Computer                 │
│                                             │
│  React Dashboard                            │
│        │                                    │
│        ▼                                    │
│  FastAPI Control Plane                      │
│        │                                    │
│        ├── Server Registry                  │
│        ├── SSH Connection Manager           │
│        ├── Remote Collector Scheduler       │
│        ├── Shared Telemetry State           │
│        └── Realtime Hub                     │
└───────────────┬─────────────────────────────┘
                │ SSH
      ┌─────────┼───────────┐
      ▼         ▼           ▼
 Server A    Server B    Server C
```

Default remote installation:

```text
No custom agent
No telemetry database
No monitoring daemon
No continuous telemetry file writes
```

---

## 3. Core Data Flow

```text
Remote Linux Server
      │
      │ SSH command
      ▼
Remote Executor
      ▼
Collector
      ▼
Normalized Models
      ▼
Shared Local Telemetry State
      ├── Latest Snapshot
      └── Bounded History
              │
        ┌─────┴─────┐
        ▼           ▼
       REST      WebSocket
                     │
                     ▼
              Frontend Store
```

---

## 4. Architectural Invariants

Unless explicitly changed through an architecture decision:

1. the local computer hosts the control plane;
2. remote servers are agentless by default;
3. SSH access is centralized;
4. SSH connections are reused;
5. API routes do not directly execute remote commands;
6. collectors do not write telemetry to disk;
7. telemetry history is bounded;
8. multiple UI clients share one collection pipeline;
9. no arbitrary shell API exists;
10. one server failure does not affect others;
11. one collector failure does not destroy unrelated telemetry;
12. the default system requires no telemetry database.

---

## 5. Backend Layering

```text
API
 ↓
Application Services
 ↓
Telemetry / Server Domain
 ↓
Collectors / Actions
 ↓
SSH Executor
 ↓
Remote OS
```

The SSH layer is infrastructure.

The collector layer converts remote command output into typed normalized data.

---

## 6. Server Registry

The server registry owns local definitions.

Conceptual server model:

```text
Server
├── server_id
├── display_name
├── ssh_alias_or_host
├── port?
├── username?
├── tags[]
├── enabled
└── capability preferences
```

Prefer using SSH config aliases.

Keep registry persistence separate from telemetry.

---

## 7. SSH Configuration Resolution

Resolution order may include:

1. explicit server override;
2. `~/.ssh/config`;
3. SSH agent;
4. referenced identity files;
5. explicit runtime credentials when necessary.

Do not copy key contents into the app database.

---

## 8. SSH Connection Manager

The connection manager owns connection state per server.

Responsibilities:

- connect;
- authenticate;
- host-key verification;
- reuse;
- reconnect;
- keepalive if needed;
- close;
- status;
- backoff;
- concurrency control.

Conceptual mapping:

```text
server_id -> managed SSH connection / bounded pool
```

---

## 9. Why Connection Reuse Matters

Do not perform:

```text
sample -> connect -> authenticate -> execute -> disconnect
```

for every sample.

Prefer:

```text
connect once
   ↓
reuse connection
   ↓
execute bounded commands
   ↓
reconnect only when needed
```

This reduces latency and CPU/network overhead.

---

## 10. Remote Executor

Collectors do not manage connection details directly.

They depend on an executor abstraction.

Conceptual interface:

```text
run(server_id, command_spec, timeout)
    -> RemoteCommandResult
```

`command_spec` should be structured when possible rather than user-provided free-form strings.

---

## 11. Collector Model

Suggested collectors:

- GPUCollector
- CPUCollector
- MemoryCollector
- DiskCollector
- NetworkCollector
- ProcessCollector
- SystemInfoCollector

Collectors:

- request remote data;
- parse output;
- normalize units;
- return typed data.

Collectors do not:

- write telemetry;
- know HTTP;
- manage browser clients;
- perform destructive actions.

---

## 12. GPU Agentless Strategy

A local process cannot hold a remote NVML handle.

Therefore agentless GPU collection uses remote commands.

Preferred approach:

```text
SSH
 ↓
nvidia-smi --query-gpu=<batched fields> --format=csv,noheader,nounits
 ↓
parse
 ↓
normalized GPU models
```

Prefer one batched query over many commands.

GPU process data may use:

```text
nvidia-smi --query-compute-apps=...
```

or another supported machine-readable query.

---

## 13. Stable and Dynamic GPU Data

Cache stable metadata longer:

- GPU name;
- UUID;
- total VRAM;
- driver version.

Sample dynamic metrics more frequently:

- utilization;
- used VRAM;
- temperature;
- power.

---

## 14. CPU / RAM Strategy

Prefer standard Linux sources without remote package installation.

Potential sources may include:

- `/proc/stat`;
- `/proc/meminfo`;
- standard utilities.

Normalize data locally.

---

## 15. Network Rate Strategy

Collect counters remotely.

Derive rates locally from:

```text
current counter - previous counter
-------------------------------
elapsed monotonic time
```

Keep only needed previous values and bounded history.

---

## 16. Process Strategy

Process collection is relatively expensive.

Use slower sampling than basic GPU utilization.

Only collect fields required by the UI.

Avoid full expensive command reconstruction unless needed.

---

## 17. Telemetry Scheduler

The scheduler decides what to collect and when.

Suggested classes:

```text
FAST
MEDIUM
SLOW
STATIC
```

It also considers:

- selected server;
- fleet-summary needs;
- active client count;
- idle mode;
- server online status.

---

## 18. Fleet Summary vs Selected-Server Detail

Do not collect maximum detail from every server continuously.

Recommended strategy:

### Fleet

Low-cost summary:

- reachability;
- GPU availability;
- CPU/RAM summary;
- major warning;
- last update.

### Selected Server

Higher-detail live telemetry:

- GPU charts;
- process details;
- network;
- storage;
- per-process GPU usage.

This materially reduces SSH load.

---

## 19. Multi-Server Concurrency

Use bounded concurrent SSH work.

Avoid launching commands against all servers without a limit.

Slow hosts should not block unrelated hosts.

Possible architecture:

```text
Global concurrency semaphore
+
per-server operation lock / limit
```

Exact implementation depends on SSH library behavior.

---

## 20. Telemetry State

Conceptually:

```text
TelemetryState
└── server_id
    ├── connection
    ├── system
    ├── cpu
    ├── memory
    ├── gpus[]
    ├── storage[]
    ├── network[]
    └── processes[]
```

Each section carries freshness metadata.

---

## 21. Bounded History

Store chart-relevant values only.

Avoid storing repeated full snapshots.

Example:

```text
GPUHistory
├── utilization
├── vram_used
├── temperature
└── power
```

Each buffer has a fixed maximum length.

---

## 22. Idle Mode

Idle mode is based on active realtime demand.

When demand drops:

- reduce sampling;
- suspend expensive selected-server collectors;
- keep low-cost fleet health.

Use a grace period to avoid mode flapping.

---

## 23. Server Offline Behavior

When a server becomes unreachable:

- mark connection state;
- stop high-frequency polling;
- apply reconnect backoff;
- preserve last snapshot only as stale;
- continue other servers normally.

---

## 24. Error Classification

Classify common errors:

- timeout;
- DNS/connect failure;
- authentication failure;
- host-key mismatch;
- permission denied;
- command missing;
- parse failure;
- unsupported capability.

This improves UI feedback and retry policy.

---

## 25. Realtime Delivery

Use one primary browser realtime connection.

The backend publishes:

- fleet summary changes;
- selected-server detailed changes;
- connection-state changes;
- action results where appropriate.

Do not push huge unchanged payloads unnecessarily.

---

## 26. Backpressure

Per-client queues are bounded.

Slow clients may skip intermediate telemetry frames.

Latest state is more important than every sample.

---

## 27. REST Responsibilities

REST may handle:

- server registry CRUD;
- connection test;
- static metadata;
- current snapshots;
- configuration;
- named actions.

Version endpoints under:

```text
/api/v1/
```

---

## 28. Frontend State

The frontend store should separate:

- fleet state;
- selected server;
- selected-server telemetry;
- connection/realtime state;
- UI preferences.

Do not duplicate the same telemetry in multiple stores.

---

## 29. Persistence Boundary

Persistence is beside, not inside, the telemetry pipeline.

```text
Server Registry / Preferences
          ↓
Persistence Interface
          ↓
Small local store
```

Telemetry path:

```text
SSH -> RAM -> browser
```

not:

```text
SSH -> database -> browser
```

---

## 30. Secrets Boundary

Secrets should not be copied into the project store.

Prefer:

- SSH Agent;
- `~/.ssh/config`;
- existing key files;
- OS secure credential storage if later needed.

Never log:

- passwords;
- key material;
- sensitive auth tokens.

---

## 31. Management Action Path

Read-only path:

```text
Collector -> Telemetry
```

Action path:

```text
Frontend
   ↓
Named API Action
   ↓
Action Service
   ↓
Validation / Capability Check
   ↓
Remote Executor
   ↓
Remote OS
```

Keep them separate.

---

## 32. No Arbitrary Shell

The browser must never submit arbitrary shell code.

The backend may internally use shell commands only behind fixed, validated action implementations.

Prefer argumentized commands and strict identifiers.

---

## 33. Capability Detection

Servers may differ.

Detect/support flags such as:

- has_nvidia;
- has_systemd;
- has_docker;
- supports_power_actions.

UI and actions should adapt.

---

## 34. Production Shape

Preferred:

```text
FastAPI process
├── API
├── SSH control plane
├── scheduler
├── realtime
└── serves built frontend assets
```

Do not require separate Node runtime in production.

---

## 35. Startup

Possible sequence:

1. load config;
2. initialize logging;
3. load server registry;
4. initialize SSH manager;
5. initialize telemetry state;
6. start scheduler;
7. start realtime hub;
8. serve API/frontend.

Do not eagerly connect to every server if that causes unnecessary startup cost; lazy or staggered connection is acceptable.

---

## 36. Shutdown

On shutdown:

- stop scheduling;
- stop realtime publication;
- close SSH connections;
- cancel reconnect tasks;
- close client connections;
- persist only explicit configuration changes if needed.

Do not dump telemetry history to disk.

---

## 37. Testing Architecture

SSH must be mockable.

Provide fake command results to collectors.

Test scenarios:

- successful server;
- offline server;
- auth failure;
- host-key mismatch;
- multi-GPU;
- no GPU;
- malformed output;
- command timeout;
- disappearing PID;
- slow client;
- bounded memory;
- many-server concurrency.

---

## 38. Resource Review

For changes affecting polling or SSH measure/inspect:

- command frequency;
- connection count;
- concurrent operations;
- local CPU;
- local RSS;
- remote command count;
- disk writes;
- realtime bandwidth.

Resource regressions are product regressions.

---

## 39. Architecture Review Checklist

Before accepting a major feature ask:

### SSH
- Does it reuse connections?
- Does it add new commands?
- Can commands be batched?
- Are timeouts defined?

### Resource
- Does it increase continuous polling?
- Is history bounded?
- Does it add concurrency?
- Can it run only for selected servers?

### Disk
- Does it create persistent files?
- Is persistence necessary?

### Security
- Can user input alter shell structure?
- Does it require sudo?
- Is the action explicit?

### Maintainability
- Is responsibility in the correct layer?
- Can it be tested without a real server?

---

## 40. Final Architecture Philosophy

Prefer:

```text
local control plane
agentless remote hosts
reused SSH connections
batched queries
fleet summaries
selected-server detail
bounded concurrency
bounded RAM
configuration-only persistence
explicit named actions
graceful degradation
```

Avoid:

```text
daemon on every host
connect-per-sample
poll-per-widget
arbitrary shell APIs
unbounded history
telemetry databases by default
unlimited multi-host concurrency
```

---

## 40a. Workspace & Transfer Layer

- workspace metadata lives beside the registry: `data/workspace.json`, atomic replace,
  written only on explicit CRUD (repository + service under `app/workspace/`);
- placement inspections run explicit SSH checks on request; observations are RAM-only;
- transfers (`app/transfer/`) never import asyncssh: they consume the transport-neutral
  `TransferSession`/`LongCommandSession` protocols from `app/ssh/file_transfer.py`;
- transfer sessions are dedicated SSH connections sharing auth/host-key policy with
  telemetry but with independent lifecycles and their own concurrency slots
  (`max_transfers_global=2`, per-server 1) — a telemetry timeout cannot kill a running
  dataset transfer and vice versa;
- Local Relay streams SFTP chunks through bounded RAM (no staging file); direct rsync
  requires a strict non-interactive preflight and falls back to relay automatically;
- transfer job state is a RAM-only state machine
  (queued→planning→running→verifying→completed|failed|cancelled), history bounded.

## 41. V3 Concrete Decisions

- SSH transport: AsyncSSH 2.x, documented in ADR-0002 (re-affirmed for v3 after GPUWatch study;
  GPUWatch lessons adopted: end-to-end timeout budget per operation, never kill an in-progress
  connection on timeout to avoid reconnect loops, per-server timeout overrides, lazy first connect);
- connection topology: one reusable managed connection per server with up to two active command
  channels; keepalives enabled; bounded exponential reconnect backoff with jitter (2-60 s);
- global in-flight SSH command limit: 8; per-server in-flight limit: 2;
- transport consumers: only `app/ssh/*` imports `asyncssh`; collectors depend on an executor protocol;
- registry persistence: atomically replaced JSON under `backend/data/`, written only on explicit CRUD;
- telemetry persistence: none; history is bounded deques in RAM (300 points per metric per GPU);
- scheduler: one central demand-aware scheduler; fleet summary 15 s active / 45 s idle;
  selected-server fast collectors 2.5 s, medium 5 s, processes 7.5 s, storage 30 s, static 10 min;
  idle mode suspends selected-only collectors after a 20 s grace period;
- realtime: one `/api/v1/realtime` WebSocket; per-client bounded latest-state queue of 2;
- frontend state: one REST client, one WebSocket client, one shared store (zustand);
- charts: bounded native SVG sparklines only (no chart library in MVP);
- icons: Phosphor only; fonts: Manrope + IBM Plex Mono self-hosted via @fontsource;
- production: FastAPI serves the built Vite output.

See `docs/IMPLEMENTATION_PLAN.md` for API, sampling, and task contracts.

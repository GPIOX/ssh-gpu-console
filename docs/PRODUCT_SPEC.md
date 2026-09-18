# PRODUCT_SPEC.md

## 1. Product Definition

Build a lightweight local application for managing remote Linux/GPU servers over SSH.

Working description:

> **A lightweight, agentless, SSH-based GPU server management workstation.**

The control plane runs on the user's own computer.

Remote servers remain agentless by default.

---

## 2. Target Users

Primary users:

- AI researchers;
- deep learning engineers;
- computer vision researchers;
- developers;
- laboratory GPU-server administrators;
- users managing several Linux workstations.

---

## 3. Primary User Problems

Users currently need to SSH into multiple servers and repeatedly use tools such as:

- `nvidia-smi`;
- `htop`;
- `free`;
- `df`;
- `ps`;
- `systemctl`;
- Docker CLI.

The product should answer, from one browser interface:

- Which servers are online?
- Which GPUs are free?
- Who is using each GPU?
- How much VRAM is available?
- Which server is overloaded?
- Is RAM or disk becoming critical?
- Which process is consuming resources?
- Can I safely terminate or manage a remote process/service?

---

## 4. Product Principles

### Agentless by Default

Remote hosts should not require a custom resident daemon.

### Lightweight

Monitoring must impose minimal overhead locally and remotely.

### Memory-First

Telemetry history stays in bounded local RAM.

### Disk-Write-by-Exception

Persist configuration only when needed.

### Multi-Server First

The root object is a server fleet, not one local machine.

### Safe Management

No browser-based arbitrary shell.

---

## 5. Non-Goals

The MVP is not:

- Kubernetes management;
- enterprise observability;
- a Prometheus/Grafana replacement;
- a general remote shell;
- a full configuration-management system;
- a data-center CMDB;
- a long-term telemetry warehouse.

---

## 6. MVP Information Architecture

```text
Servers
├── Fleet Overview
└── Selected Server
    ├── Overview
    ├── GPUs
    ├── Processes
    ├── System
    ├── Storage
    ├── Network
    └── Settings / Capabilities
```

Optional later sections:

- Services
- Containers
- Power actions
- Alerts

---

## 7. Server Registry

Users can define managed servers.

A server record may contain:

- display name;
- SSH config alias or host;
- port if needed;
- username if needed;
- tags;
- enabled/disabled state;
- optional capability flags.

Prefer importing/reusing existing `~/.ssh/config` configuration.

Do not store plaintext SSH passwords.

---

## 8. Fleet Overview

The Fleet page should answer quickly:

- Which servers are online?
- Which are offline or degraded?
- Which servers have available GPUs?
- Which servers are under CPU/RAM pressure?
- When was each server last updated?

Potential per-server summary:

- name;
- online status;
- GPU summary;
- CPU utilization;
- RAM utilization;
- disk warning;
- last updated time;
- tags.

---

## 9. Server Detail Overview

For one selected server show:

### Identity

- display name;
- SSH alias/host;
- OS;
- uptime;
- connection status.

### CPU

- utilization;
- load;
- model/core summary.

### Memory

- used / available / total;
- utilization percentage.

### GPU

For each GPU:

- index;
- name;
- UUID where available;
- utilization;
- used/total VRAM;
- temperature;
- power;
- process count.

### Storage

- important mounts;
- used/free;
- warnings.

### Network

- RX/TX rate.

### Top Processes

Compact list of resource-heavy processes.

---

## 10. GPU Page

GPU monitoring is a primary feature.

Support:

- no GPU;
- one GPU;
- multi-GPU systems.

Per-GPU data may include:

- name;
- UUID;
- utilization;
- VRAM;
- temperature;
- power;
- power limit;
- fan;
- clocks;
- active compute processes.

Unsupported fields show `N/A` rather than failing the page.

---

## 11. GPU History

Provide short-term charts for:

- utilization;
- VRAM;
- temperature;
- power.

History is:

- bounded;
- local;
- in memory;
- disposable on restart.

Typical useful window:

- approximately 5–15 minutes.

No telemetry database in MVP.

---

## 12. GPU Processes

Display:

- PID;
- user;
- process name;
- command where useful;
- GPU;
- GPU memory.

Correlate with general process data where practical.

---

## 13. Processes Page

Use a dense table.

Potential columns:

- PID;
- user;
- name;
- CPU;
- RAM;
- GPU;
- GPU memory;
- state;
- command.

Support:

- sort;
- search;
- filters;
- GPU-only filter.

---

## 14. Process Actions

Initial actions may include:

- terminate;
- kill.

Requirements:

- explicit target server;
- explicit PID;
- confirmation;
- backend validation;
- status feedback.

Never accept arbitrary shell text.

---

## 15. System Page

Display useful system details:

- hostname;
- OS/kernel;
- uptime;
- CPU model;
- physical/logical CPU;
- RAM;
- load.

Do not become a full inventory system.

---

## 16. Storage Page

Display:

- device;
- filesystem;
- mount point;
- total;
- used;
- free;
- utilization.

Storage capacity is more important than high-frequency disk I/O for MVP.

---

## 17. Network Page

Display:

- relevant interfaces;
- IP information where useful;
- RX/TX rate.

Keep history bounded.

---

## 18. Realtime Model

Fleet summaries and selected-server details should update automatically.

The frontend uses shared realtime state.

Do not create polling per widget.

Do not create detailed high-frequency subscriptions for every server unless needed.

---

## 19. Sampling

Suggested interactive defaults:

| Data | Approx. interval |
|---|---:|
| GPU utilization / VRAM | 2 s |
| CPU / RAM | 2–3 s |
| GPU temperature / power | 3 s |
| Network | 3 s |
| GPU processes | 3–5 s |
| General process list | 5 s |
| Storage capacity | 15–30 s |
| Static metadata | once / long cache |

Idle mode should reduce these frequencies.

---

## 20. Idle Mode

When no active clients need live details:

- lower sampling;
- reduce process enumeration;
- suspend optional collectors;
- keep only lightweight health checks where useful.

---

## 21. SSH Experience

Users should be able to reuse their existing SSH environment.

Preferred onboarding:

- select/import SSH config alias;
- validate connection;
- show host-key/auth errors clearly;
- save only necessary server metadata.

Do not require copying private keys into the application.

---

## 22. Error States

The product must handle:

- host offline;
- timeout;
- authentication failure;
- host-key mismatch;
- missing `nvidia-smi`;
- no NVIDIA GPU;
- unsupported GPU field;
- permission denied;
- process disappeared;
- missing systemd;
- missing Docker.

One server failure must not affect other servers.

---

## 23. Security

Never provide a generic shell endpoint.

Dangerous operations require:

- named backend action;
- validated arguments;
- explicit target;
- clear confirmation;
- privilege awareness.

---

## 24. Persistence

MVP persistence may include:

- server registry;
- tags;
- saved preferences.

Telemetry should not be persisted.

Secrets should use existing SSH agent/config/keychain mechanisms where possible.

---

## 25. Frontend Design

Goals:

- technical;
- compact;
- polished;
- high information density;
- strong dark mode;
- clear server identity.

Avoid generic AI-dashboard aesthetics.

---

## 26. Long-Running Stability

The dashboard may remain open for days.

The system must avoid:

- unbounded frontend history;
- duplicate WebSocket handlers;
- timer leaks;
- backend queue growth;
- SSH connection leaks;
- endless retry loops.

---

## 27. Production Deployment

Preferred production shape:

```text
Local Computer
└── FastAPI
    ├── API
    ├── SSH control plane
    └── built frontend static assets
```

Avoid requiring a permanent Node dev server.

---

## 28. MVP Acceptance Criteria

The MVP is successful when the user can:

1. open a local browser UI;
2. view all configured servers;
3. identify online/offline state;
4. select a server;
5. inspect GPU/CPU/RAM/storage/network;
6. see GPU processes;
7. inspect general processes;
8. safely terminate a process;
9. keep the UI open for long periods without memory growth;
10. manage several servers without multiplying SSH load per browser.

Additionally:

- remote custom agent is not required;
- telemetry is memory-only;
- normal monitoring causes no continuous telemetry disk writes;
- SSH connections are reused;
- offline hosts are isolated;
- systems without GPUs are supported;
- frontend is visually polished and maintainable.

---

## 28a. Workspace Management & Cross-server Transfer

The product manages servers; workspaces manage the AI assets ON them.

- **Workspace catalog**: projects, artifacts (code / dataset / model / immutable versions),
  placements (which server, which path), launch configs (structured, no shell strings).
- **Cross-server transfer MVP**: rsync-based direct sync with automatic Local Relay fallback
  (bounded RAM chunks through the control plane), quick size verification, explicit cancel/retry.
- **Placement inspection is explicit**: user-requested SSH checks only, results RAM-only.
- **Credentials & direct transfer (Phase 4.2)**: optional local password in a secure OS
  keyring (session-only RAM fallback, never persisted or logged); server→server direct
  rsync via native BatchMode auth when available, else an explicitly user-provisioned,
  pair-scoped, revocable ED25519 dedicated key — a password is never used for
  server-to-server rsync, and unavailable direct auth falls back to Local Relay.
- **Runtime state is never persisted**: transfer progress, scan results and job history stay
  in bounded RAM; workspace.json only changes on explicit CRUD.
- Destructive operations on remote files are NOT in scope. No mirror/delete sync.

## 29. Future Scope

Possible future additions:

- Services;
- Docker/Podman;
- reboot/shutdown;
- Wake-on-LAN where applicable;
- alerts;
- remote terminal as a separately secured feature, if ever needed;
- optional remote lightweight agent for advanced metrics;
- multi-user auth;
- remote-site grouping;
- optional long-term history.

Do not complicate MVP for these future ideas.

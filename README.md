# SSH GPU Console

[English](./README.md) | [简体中文](./README.zh-CN.md)

A lightweight, agentless, SSH-based GPU server management workstation.

The control plane runs **on your computer**: a FastAPI backend manages any number of remote
Linux GPU servers over reused SSH connections, and a React dashboard renders the fleet and
per-server telemetry. Remote servers need **no agent, no daemon, no database, and no
continuous telemetry writes** — the product observes servers without becoming a workload.

```
My Computer
├── React + TypeScript dashboard
├── FastAPI local control plane
│    ├── Server Registry (JSON, written only on change)
│    ├── SSH Connection Manager (AsyncSSH, reused connections)
│    ├── Remote Executor (bounded concurrency, timeouts)
│    ├── Demand-aware Collector Scheduler
│    ├── Shared Telemetry State (bounded RAM only)
│    └── Realtime Hub (one WebSocket, backpressure)
└── SSH ──▶ Linux GPU Server A / B / C   (agentless)
```

## Features

- **Fleet view**: all servers as live machine panels — status, CPU/RAM meters, per-GPU lanes
  (utilization, VRAM, temperature, power, availability, owning users), disk warnings,
  freshness.
- **Server detail**: Overview, GPUs, Processes, System, Storage, Network — sections you can
  hide (Processes / System / Storage / Network) if you never look at them.
- **GPU-first**: multi-GPU support, bounded history sparklines, GPU compute processes
  correlated with system processes.
- **Onboarding**: import aliases from `~/.ssh/config`, test connections with explicit error
  taxonomy, explicit host-key trust (TOFU) stored app-side — your `known_hosts` is never
  modified.
- **Your console, your way**: dark and light themes (Minimal & Warm, system-aware),
  English / 简体中文 UI, per-server sampling interval (0.5–600 s).
- **Named actions only**: process terminate/kill with explicit confirmation. No shell API.
- **Frugal by design**: connection reuse, batched queries, bounded memory, idle-mode
  sampling, disk-write-by-exception.

## Requirements

- Python 3.12+ (uv recommended)
- Node 20+ and pnpm
- SSH access to your servers via your existing OpenSSH environment (agent, config, keys)

## Development

```bash
# backend
cd backend
uv venv .venv && uv pip install -e ".[dev]" -p .venv/bin/python
.venv/bin/uvicorn app.main:app --port 8420

# frontend (separate terminal)
cd frontend
pnpm install
pnpm dev            # http://localhost:5173, proxies /api to :8420
```

## Production

```bash
cd frontend && pnpm build
cd backend && .venv/bin/uvicorn app.main:app --port 8420   # serves frontend/dist
```

## Testing / quality gates

```bash
cd backend && .venv/bin/python -m ruff check app tests && .venv/bin/python -m mypy app \
  && .venv/bin/python -m pytest -q
cd frontend && pnpm lint && pnpm typecheck && pnpm test -- run && pnpm build
```

## Roadmap

- [ ] Unified management of projects, datasets, models and launch configurations across servers
- [ ] Cross-server sync for project material

## Documentation

- `docs/PRODUCT_SPEC.md` — what the product is and is not
- `docs/ARCHITECTURE.md` — invariants, layering, data flow
- `docs/IMPLEMENTATION_PLAN.md` — API contract, sampling policy, remote command specs
- `docs/adr/` — architecture decisions (transport, control plane)

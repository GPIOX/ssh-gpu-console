# Workspace Management & Cross-server Transfer — Design Contract (v3)

Status: active contract for the Workspace (Phase 1) and Transfer (Phase 2) features.
Architecture ownership unchanged: module boundaries, wire models, sampling/persistence
boundaries and acceptance are maintainer-owned; scoped changes only.

## 1. Scope of this phase

ADDED: workspace catalog (projects / artifacts / placements / launch configs / server
roots), placement inspection, cross-server transfer MVP (local relay + direct rsync +
auto planner).

NOT in this phase: tmux training launch, experiment tracking, docker sync, deployment,
remote terminal, alerts, remote agents, telemetry persistence, auto-deployment, mirror
sync, destructive remote deletes, scp as core transport.

## 2. Data model

Persistent declaration (workspace.json, written only on explicit CRUD):

```text
ProjectRecord       project_id, name, description, artifact_ids[], launch_config_ids[], tags[], created_at, updated_at
ArtifactRecord      artifact_id, kind(code|dataset|model), name, version?, description?, immutable, created_at, updated_at
PlacementRecord     placement_id, artifact_id, server_id, remote_path, created_at, updated_at
LaunchConfigRecord  launch_config_id, project_id, name, working_dir?, program, args[], environment?, env_vars{}, required_artifact_ids[], gpu_count?, min_vram_b?, created_at, updated_at
ServerRoots         server_id → { project_root?, dataset_root?, model_root?, output_root? }
```

Rules:

- datasets/models are independent artifacts referenced by projects (`artifact_ids`) — never
  embedded, because one artifact (e.g. DINOv2-B) serves many projects;
- datasets/models default `immutable=true`; changing content = create a new version;
  code may be mutable;
- LaunchConfig is STRUCTURED (`program` + `args[]`), never a shell string;
- field bounds: ids 1–64, names ≤120, descriptions ≤600, remote paths ≤512, tags ≤16,
  args ≤64 items; `extra="forbid"` everywhere;
- referential integrity: project.artifact_ids must exist; deleting a referenced artifact is
  rejected (409); deleting a placement removes metadata only and NEVER touches remote files;
  deleting a project removes its launch configs (config data) and keeps artifacts/placements;
  removing a server from the registry leaves placements in place (marked unavailable at
  read time by API since server is unknown — no cascade).

Runtime observation (RAM only, never persisted): placement scan results, verified_at cache,
transfer progress/speed/ETA, active job state.

## 3. Persistence boundary

`backend/data/workspace.json` via the existing atomic JsonFileStore:

```json
{"schema_version": 1, "projects": [], "artifacts": [], "placements": [], "launch_configs": [], "server_roots": {}}
```

Written ONLY on explicit CRUD. No background periodic saves. No telemetry writes.
Transfer jobs are RAM-only objects; no transfers.json, no transfer history files.

## 4. Runtime integration

`Runtime` extends to `settings, ssh, telemetry, workspace, transfers`. The composition root
(`core/lifecycle.py build_context()`) constructs `WorkspaceRepository` (JsonFileStore-backed),
`WorkspaceService`, `TransferService`; API routes resolve them via `get_runtime()`.
`AppContext.start()` starts transfer workers; `AppContext.stop()` stops transfers first,
then closes SSH.

## 5. API surface (`/api/v1`)

Workspace: CRUD for `/workspace/projects`, `/workspace/artifacts`,
`/workspace/placements`, `/workspace/launch-configs` (+ `POST
/workspace/placements/{id}/inspect`), `GET/PUT /workspace/server-roots`.
Inspect runs an explicit SSH check (exists / type / size / basic file count) only on user
request; results live in RAM.

Transfers: `GET /transfers`, `GET /transfers/{job_id}`, `POST /transfers/plan`,
`POST /transfers`, `POST /transfers/{id}/cancel`, `POST /transfers/{id}/retry`.
Listing reads RAM only and never triggers SSH. Frontend polls (~1 Hz) only while a job is
queued/planning/running/verifying.

## 6. Transfer isolation (security & stability rules)

- telemetry semaphores (`max_in_flight_global/per_server`) are NOT used for transfers;
  transfers have their own slots (`max_transfers_global=2`, `max_transfers_per_server=1`);
- transfers use their own SSH/SFTP sessions — a telemetry timeout must never kill a running
  dataset transfer; they still reuse server auth/config/host-key policy/agent/trust rules;
  no private keys copied, no host-key checking disabled, no agent forwarding by default;
- only `app/ssh/transport.py` imports asyncssh; `app/ssh/file_transfer.py` defines the
  transport-neutral `TransferSession` protocol (stat/mkdir/open_reader/open_writer/rename/
  remove/close); `app/transfer/*` never imports asyncssh;
- all paths/hosts in generated commands pass a dedicated safe-quoting builder; user
  supplied rsync flags are forbidden; flags are fixed backend-side;
- rsync: `--partial --partial-dir=.sgc-rsync-partial` for resume, NEVER `--delete`, no
  `--append` by default; direct mode requires non-interactive (BatchMode) source→target SSH
  and rsync on both ends — preflight failure falls back to Local Relay (auto), never fails
  the transfer for lack of direct routing, and never copies credentials to make it work;
- Local Relay: SFTP chunked copy through a bounded RAM buffer (`transfer_chunk_size_b`,
  default 4 MiB), NO local staging file; copy/update semantics (no mirror, no remote
  deletes); partial target files are written as `.<name>.sgc-partial-<job_id>` then renamed;
  symlinks are skipped (no path escape); quick verification = size / transferred bytes /
  basic file count (no full-dataset hashing in this phase);
- state machine: queued → planning → running → verifying → completed | failed | cancelled;
  retry re-queues; completed/failed history bounded (`transfer_job_history=100`);
- transfer excludes: `ProjectRecord.transfer_excludes` (≤32 single-line rsync/fnmatch
  patterns, editable in the UI and PATCHable) is the project-level DEFAULT; the effective
  list for a job is the UNION over the projects referencing the artifact (the copy can
  only shrink, never grow); rsync receives them as `--exclude=arg` (full rsync semantics,
  the transfer root itself is never excluded); the relay matches entry NAMES during the
  walk (`fnmatch`, dot entries `.`/`..` are always filtered); the effective list is shown
  in the plan preview and carried on the job (`TransferPlan.excludes`/`TransferJob.excludes`).

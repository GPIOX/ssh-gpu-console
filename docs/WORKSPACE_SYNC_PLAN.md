# Workspace Management & Cross-server Transfer — Design Contract (v3)

Status: active contract for the Workspace (Phase 1) and Transfer (Phases 2–3) features;
Phase 4 (project distribution & reconciliation) is complete (§8).
Architecture ownership unchanged: module boundaries, wire models, sampling/persistence
boundaries and acceptance are maintainer-owned; scoped changes only.

## 1. Scope of this phase

ADDED: workspace catalog (projects / artifacts / placements / launch configs / server
roots), placement inspection, cross-server transfer MVP (local relay + direct rsync +
auto planner).

NOT in this phase: tmux training launch, experiment tracking, docker sync, deployment,
remote terminal, alerts, remote agents, telemetry persistence, auto-deployment, mirror
sync, destructive remote deletes, scp as core transport.

Phase 3 ADDED (this document is current with it): resumable relay transfers
(deterministic partial + meta sidecar), incremental sync (skip up-to-date files,
preserve source mtimes), symlink-safe tree walks with bounded warnings, a per-target
write lock, a hard transfer-space preflight, rsync flag/cancel hardening, and richer
job bookkeeping fields (§7).

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
`POST /transfers`, `POST /transfers/{id}/cancel`, `POST /transfers/{id}/retry`,
`DELETE /transfers` (clears the bounded terminal-state history; active jobs are kept).
Listing reads RAM only and never triggers SSH. Frontend polls (~1 Hz) only while a job is
queued/planning/running/verifying.

## 6. Transfer isolation (security & stability rules)

- telemetry semaphores (`max_in_flight_global/per_server`) are NOT used for transfers;
  transfers have their own slots (`max_transfers_global=2`, `max_transfers_per_server=1`);
- transfers use their own SSH/SFTP sessions — a telemetry timeout must never kill a running
  dataset transfer; they still reuse server auth/config/host-key policy/agent/trust rules;
  no private keys copied, no host-key checking disabled, no agent forwarding by default;
- only `app/ssh/transport.py` imports asyncssh; `app/ssh/file_transfer.py` defines the
  transport-neutral `TransferSession` protocol (stat / lstat / listdir / mkdir /
  open_reader(offset) / open_writer(offset, truncate) / set_mtime / rename / remove /
  close) and the `FileStat` record (size_b, is_symlink, mtime_s — SFTPv3 exposes mtime
  only in whole seconds); `app/transfer/*` never imports asyncssh;
- all paths/hosts in generated commands pass a dedicated safe-quoting builder; user
  supplied rsync flags are forbidden; flags are fixed backend-side;
- rsync flags are exactly `-r -l -t -p --safe-links --partial --partial-dir=.sgc-rsync-partial
  --info=progress2` (no `-a`/`-o`/`-g`: owner/group preservation would fail for
  unprivileged users; `-l` copies symlinks AS links, `--safe-links` makes the receiver
  refuse absolute or parent-escaping targets, `-t` keeps mtimes for later incremental
  syncs). The target port travels ONLY via `-e "ssh -p PORT"`; the remote spec never
  embeds a port (rsync treats everything after the first colon of a remote spec as path).
  Both the BatchMode probe and the `-e` ssh options use `-o StrictHostKeyChecking=yes`
  (never `accept-new`, never writes to the source's known_hosts — an untrusted target
  just fails the probe). `--delete` and `--append`/`--append-verify` are NEVER sent.
  Direct mode still requires non-interactive (BatchMode) source→target SSH and rsync on
  both ends — preflight failure falls back to Local Relay (auto), never fails the
  transfer for lack of direct routing, and never copies credentials to make it work;
- rsync cancellation is a real race, not a flag: the service runs the remote command
  against the job's cancel event; when cancel wins, the command session is closed so the
  remote sshd terminates rsync immediately, and `--partial-dir` preserves the remote
  partial for a later resume;
- target write lock: the job registry allows at most ONE active job per
  (target_server_id, normalized target_path); a second job targeting the same path is
  rejected with 409 until the holder reaches a terminal state (the relay's shared
  partial/meta scratch depends on this — two concurrent writers would corrupt it);
- Local Relay: SFTP chunked copy through a bounded RAM buffer (`transfer_chunk_size_b`,
  default 4 MiB), NO local staging file; copy/update semantics (no mirror, no remote
  deletes); the partial/meta/resume/incremental/symlink contract is §7;
- quick verification is size/count parity only — NO full-dataset hashing in any phase;
  details in §7;
- state machine: queued → planning → running → verifying → completed | failed | cancelled;
  retry re-queues the finished job's parameters as a NEW job; completed/failed/cancelled
  history bounded (`transfer_job_history=100`);
- transfer excludes: `ProjectRecord.transfer_excludes` (≤32 single-line rsync/fnmatch
  patterns, editable in the UI and PATCHable) is the project-level DEFAULT; the effective
  list for a job is the UNION over the projects referencing the artifact (the copy can
  only shrink, never grow); rsync receives them as `--exclude=arg` (full rsync semantics,
  the transfer root itself is never excluded); the relay matches entry NAMES during the
  walk (`fnmatch`, dot entries `.`/`..` are always filtered); the effective list is shown
  in the plan preview and carried on the job (`TransferPlan.excludes`/`TransferJob.excludes`).

## 7. Relay resume, incremental sync & job bookkeeping (Phase 3 contract)

- deterministic scratch names: every relayed file is staged on the target as
  `<dir>/.<name>.sgc-partial` — NO job_id in the name, so the partial written by one job
  is reusable by whichever later job wins the same target lock — with a JSON meta sidecar
  `<dir>/.<name>.sgc-meta` written (truncate) BEFORE the first byte. The sidecar carries
  exactly five keys: `artifact_id`, `source_path`, `target_path`, `source_size_b`,
  `source_mtime_s`;
- success consumes the scratch: the partial is renamed onto the target name, then the
  meta sidecar is removed. Cancel/failure keeps BOTH files in place for a later retry;
- resume validation (ONE set of rules for code and for dataset/model — there is no
  "safe to stitch" class): the partial is appended to ONLY when the meta matches THIS
  transfer on all five keys — including the CURRENT source size+mtime — and
  0 < partial size ≤ source size. A changed source, code or dataset, is always
  retransferred from zero, never stitched onto stale bytes. Because the names are
  deterministic and the sidecar lives on the target, a NEW job — including after a
  backend restart, with no in-RAM job state — resumes directly from the leftovers;
- incremental sync: a target file whose size AND mtime equal the source is skipped
  without reading the source and counted in `files_skipped`/`bytes_skipped` (unknown
  mtime, i.e. 0, never skips). After a successful rename the source mtime is preserved
  via `set_mtime`; SFTPv3 encodes ACMODTIME as an atime+mtime PAIR, so one value is
  written to both fields — passing mtime alone silently no-ops (field-verified on
  OpenSSH sftp-server). set_mtime failures are suppressed (best-effort metadata);
- lstat/symlink safety: directory walks use lstat and NEVER follow or copy symlinks
  found inside the tree — each is skipped and recorded in `warnings` (bounded, ≤20
  entries). A single-file ROOT is stat()ed (follows symlinks): the root is user
  declared, so the content it points at is what transfers;
- space preflight: the plan carries `source_size_b` (`du -sb` for directories,
  `stat -c %s` for files), `target_free_b` (`df -B1` on the target's parent directory)
  and a `space_warning` for the plan preview. At run time, clearly insufficient space
  AND a missing target → job FAILED (`insufficient_space`); an existing target → warning
  only (incremental data may need less); any probe failure never blocks the job;
- job bookkeeping fields (RAM-only, defaulted so existing constructors keep working):
  `immutable` (dataset/model roots must not be mutated by a later copy; code may; an
  explicit per-artifact override wins), `strategy_reason` (plan reason for job detail
  views), `resumed_bytes` (bytes taken over from an existing partial), `files_skipped`
  / `bytes_skipped` (incremental-sync skips), `warnings` (bounded list, e.g. skipped
  symlink reasons);
- quick verify (no SHA256 in this phase): single file = target size parity with the
  source (and non-empty); directories = the job's own counters when files AND bytes were
  fully tracked, otherwise a re-walk of the source requiring every currently-existing
  source file to exist at the mirrored target path with a matching size (a source file
  that changed size after being copied fails on purpose). Extra target files —
  pre-existing files or `.sgc-partial` residue — never fail verification (copy/update
  semantics).

## 8. Phase status

- Phase 1 (workspace catalog & placement inspection) — complete.
- Phase 2 (transfer MVP: local relay + direct rsync + auto planner) — complete.
- Phase 3 (resume, incremental sync, symlink/target-lock/space/rsync hardening) —
  complete.
- Phase 4 (project distribution & reconciliation) — complete. Delivered: the
  distribution read model, sync planning, batch sync and the matrix/dialog
  frontend.
- Phase 4.2 (SSH credentials & direct transfer setup) — complete. Delivered:
  CredentialStore (secure OS keyring / session-only), optional local password
  auth, server→server direct-auth lifecycle (native → dedicated ED25519 key
  → unavailable/relay), dedicated-key setup/repair/revoke, app-owned remote
  known_hosts, direct rsync integration and the auth/direct-transfer UI.
  Details: `docs/SSH_CREDENTIALS_AND_DIRECT_TRANSFER.md`. Real-machine LEVEL 2
  was executed with explicit user authorization (setup → sgc_key direct
  rsync smoke → no-op rerun → revoke → authentication_failed → relay
  fallback), with all user key material preserved byte-for-byte.

---
name: workspace-fill
description: >-
  Fill the SSH GPU Console workspace from real servers — register projects,
  code/dataset/model artifacts, placements, per-server roots and structured
  launch configs, then prepare cross-server syncs. Use whenever the user asks
  to 登记/填写工作区, add datasets/models/weights/启动配置 to the console, set
  服务器根目录, verify what is already cataloged, or prepare syncing project
  material from one server to another — even if they only say "帮我把这些数据集
  和权重填进工作区" or "把这套东西同步到另一台机器".
---

# Fill the workspace from real servers

Onboard a research project into the SSH GPU Console workspace: catalog its
code, datasets, model runs and launch configs against real servers, and get it
ready for a cross-server sync. All writes go through the LOCAL control plane
API (http://127.0.0.1:8420/api/v1) — never hand-edit `backend/data/workspace.json`.

## Three rules that matter most

1. **Verify every remote path on the real server before writing it into the
   catalog.** Use read-only SSH (`ssh -o BatchMode=yes -o ConnectTimeout=10 '<alias>'
   'ls … ; du -sh …'`) to see the real tree. Never trust the user's shorthand:
   `$CODE_ROOT/dataset/{A,B}` is shell syntax the product does not evaluate —
   expand it to one concrete path per entry yourself, and confirm each one
   exists on the actual server.
2. **Placements are declarations.** Creating a placement never copies anything
   and never touches remote files; a transfer only happens when the user
   triggers 同步 in the UI (or POST /workspace transfers API).
3. **Never let real identifiers leak into committed files.** Catalog data
   (workspace.json) lives on this machine only, but anything written into the
   repo (docs, tests, scripts, skills) must use synthetic placeholders —
   Server A / Server B, Dataset D1 / D2, Model M1 / M2, /home/demo/…

## Workflow

### 1. Read the current state (always first)

```bash
curl -s http://127.0.0.1:8420/api/v1/servers | jq 'map({display_name, server_id, ssh_host, enabled})'
curl -s http://127.0.0.1:8420/api/v1/workspace/projects | jq 'map({name, artifact_ids, launch_config_ids})'
curl -s http://127.0.0.1:8420/api/v1/workspace/artifacts  | jq 'map({name, version, kind})'
curl -s http://127.0.0.1:8420/api/v1/workspace/placements | jq 'map({server_id, remote_path})'
```

Map `display_name → server_id` once and reuse it; the workspace API only
speaks `server_id`.

### 2. Survey the real servers (read-only SSH)

On the SOURCE server: list the code root (`ls <root>`), dataset directories,
checkpoint/run directories; `du -sh` each dataset and run dir for sizes.
On the TARGET server: `ls ~` for the layout convention and `df -h /` for free
space — compare against the total you are about to copy.
For launch configs: find the real training entry scripts and grep their
argparse (read-only `grep -n "add_argument" script.py`) — copy the real flags
and defaults; never invent CLI args. A run script in the repo (e.g. one bash
script per experiment) is the best source of the exact command line.

### 3. Design the catalog

- One **artifact** per independently syncable entity: the code root, each
  dataset (`D1`, `D2`…), each model run worth moving (`M1`, `M2` — same
  `name` with different `version` for run variants, e.g. ablation flags or
  seed). Datasets stay `immutable: true` (the default); code is mutable.
- One **placement** per (artifact, server) where it physically lives — absolute
  paths or `~`-based; stored verbatim, never shell-expanded.
- Attach artifacts to the **project** (PATCH `artifact_ids`).
- **Server roots** on BOTH the source and the target server: the target's
  roots decide the sync target-path prefill (`<root>/<name:version>`), per
  kind: dataset → `dataset_root`, model → `model_root`, code → `project_root`.
- **Launch configs** are structured `program + args` (never shell strings):
  one config per experiment; reuse real script content for `program`/`args`;
  `environment` is the conda env name; `required_artifact_ids` lists the
  artifacts the run consumes (code + datasets it trains on).

### 4. Fill it in

Adapt `scripts/fill_workspace.py` (idempotent: it looks up existing entries
before POSTing, so a partial run can be re-run safely) with the SPEC you
verified in steps 1–2, then run it with plain `python3` (stdlib only).
It creates artifacts + placements, attaches them to the project, saves roots
for every server in the SPEC, and creates launch configs.

### 5. Verify

Inspect every placement — it `stat`s the real remote path:

```bash
curl -s -X POST http://127.0.0.1:8420/api/v1/workspace/placements/<id>/inspect | jq .
```

Expect `"state": "verified"` everywhere; `missing`/`unavailable` means the
path or the server needs attention before any sync.

### 6. Hand back to the user

Tell them: 工作区 → 条目行 → 同步 opens the New-Transfer dialog with the
source placement pre-filled and the target path pre-filled from the target
server's roots. Recommend syncing the smallest dataset first as a trial, then
the rest. Call out anything that would sync more than intended — see the
first caution below.

## Field-tested cautions

- **Code artifacts can be heavy.** A code placement covering the whole project
  tree also covers nested `dataset/` and `checkpoints/` subdirectories; the
  transfer MVP has no exclude list, so syncing "code" would drag ~all data
  along. If the user only needs code, ask for a split (register the code tree
  minus heavy dirs, or extend the transfer planner with excludes first).
- **Model run dirs** (`log_*`-style) usually bundle tensorboard + training
  logs + `weights/`. Registering the whole run dir as one model artifact is
  fine for archiving; name the placement at the run dir, not at loose files.
- **Versions appear in prefill.** Target paths prefill as
  `<root>/<name:version>` — a version like `ablation-variant1` yields a colon
  in the suggested directory name. Datasets (no version) prefill clean; for
  model runs the user may want to edit the target path before confirming.
- **Validation gotchas.** `program`/`working_dir` reject shell metacharacters
  (`; & | $ ( ) { }` etc. — see `app/models/workspace.py`); remote paths are
  ≤512 chars; args ≤64 tokens. `launch_config_ids` on a project is DERIVED at
  read time — never try to PATCH it.
- **Sync needs trust, not just declaration.** The target server must be added
  and online (host key trusted) before a transfer can run; the first sync of
  a big dataset is a good stress test of relay/strategy behavior.

#!/usr/bin/env python3
"""Fill the SSH GPU Console workspace from a verified SPEC (local API on :8420).

Edit the SPEC block, verify every path on the real server first (read-only
SSH — see SKILL.md), then run with plain ``python3`` (stdlib only).

Idempotent: entries are looked up by (name, version) / (artifact, server) /
config name before POSTing, so a partial run can be re-run safely. Set
``APPLY = True`` to actually write; the default dry-run prints the plan.

Synthetic example below: Server A / Server B, Dataset D1 / D2, Model M1 / M2,
/home/demo/... — replace with the real values you verified, never paste real
usernames/hosts into committed files.
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8420/api/v1"
APPLY = "--apply" in sys.argv

# ---- SPEC — synthetic example; replace with verified values -----------------

# Console display names → keys used in this SPEC.
SERVERS = ["Server A", "Server B"]

PROJECT = {"name": "My-Project", "description": "one-line project description"}

ARTIFACTS = [
    # (kind, name, version, description, {server: remote_path})
    ("code", "Project-Code", None, "main code: train/eval/ablation entry points", {
        "Server A": "/home/demo/projects/app",
    }),
    ("dataset", "D1", None, "primary dataset (~12 GB)", {
        "Server A": "/home/demo/projects/app/data/D1",
    }),
    ("dataset", "D2", None, "secondary dataset (~1 GB)", {
        "Server A": "/home/demo/projects/app/data/D2",
    }),
    ("model", "M1", "ablation-variant1", "run dir: tb + training log + weights (~1.5 GB)", {
        "Server A": "/home/demo/projects/app/runs/log_D1_variant1",
    }),
    ("model", "M1", "seed42", "seed rerun of the winning config (~1.5 GB)", {
        "Server A": "/home/demo/projects/app/runs/log_D2_seed42",
    }),
    ("model", "M2", "calibrated", "calibrated adapter bundle", {
        "Server A": "/home/demo/projects/app/runs/calibrated",
    }),
]

ROOTS = {
    "Server A": {  # source: where things live today
        "project_root": "/home/demo/projects",
        "dataset_root": "/home/demo/projects/app/data",
        "model_root": "/home/demo/projects/app/runs",
        "output_root": "/home/demo/projects/app/output",
    },
    "Server B": {  # target: the convention you want there
        "project_root": "/home/demo/projects",
        "dataset_root": "/home/demo/data",
        "model_root": "/home/demo/runs",
        "output_root": "/home/demo/output",
    },
}

LAUNCH_CONFIGS = [
    # (name, working_dir, program, args, [required artifact keys], gpu_count)
    ("Ablation D1 · variant1", "/home/demo/projects/app", "train_d1_variant.py",
     ["--ablation", "variant1", "--dataset_path", "./data/D1"],
     ["code:Project-Code", "dataset:D1"], 1),
    ("D2 · seed42 full run", "/home/demo/projects/app", "train_d2_full.py",
     ["--seed", "42", "--lr", "6.3e-5", "--nepochs", "200",
      "--log_dir", "./runs/log_D2_seed42"],
     ["code:Project-Code", "dataset:D2"], 1),
]

ENVIRONMENT = "env-name"


# ---- helpers ----------------------------------------------------------------

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        payload = resp.read()
        return json.loads(payload) if payload else None


def main() -> None:
    servers = {s["display_name"]: s["server_id"] for s in call("GET", "/servers")}
    for name in SERVERS:
        if name not in servers:
            sys.exit(f"server {name!r} is not registered — add it in the console first")
    server_ids = {name: servers[name] for name in SERVERS}
    print("servers:", {k: v for k, v in server_ids.items()})

    existing_artifacts = {
        (a["name"], a["version"]): a["artifact_id"]
        for a in call("GET", "/workspace/artifacts")
    }
    existing_placements = {
        (p["artifact_id"], p["server_id"], p["remote_path"])
        for p in call("GET", "/workspace/placements")
    }

    artifact_ids: dict[str, str] = {}
    for kind, name, version, desc, placements in ARTIFACTS:
        key = f"{kind}:{name}" + (f":{version}" if version else "")
        record = existing_artifacts.get((name, version))
        if record is None:
            print(f"{'[apply]' if APPLY else '[plan] '} artifact {kind} {name}"
                  + (f":{version}" if version else ""))
            if APPLY:
                record = call("POST", "/workspace/artifacts", {
                    "kind": kind, "name": name, "version": version, "description": desc,
                })["artifact_id"]
            else:
                record = f"NEW-{key}"
        else:
            print(f"= artifact exists: {name}" + (f":{version}" if version else ""))
        artifact_ids[key] = record
        for server_name, remote_path in placements.items():
            triple = (record, server_ids[server_name], remote_path)
            if triple in existing_placements:
                print(f"= placement exists: {server_name} {remote_path}")
                continue
            print(f"{'[apply]' if APPLY else '[plan] '} placement {server_name} -> {remote_path}")
            if APPLY:
                call("POST", "/workspace/placements", {
                    "artifact_id": record,
                    "server_id": server_ids[server_name],
                    "remote_path": remote_path,
                })

    project = next(
        (p for p in call("GET", "/workspace/projects") if p["name"] == PROJECT["name"]), None
    )
    if project is None:
        print(f"{'[apply]' if APPLY else '[plan] '} project {PROJECT['name']}")
        if APPLY:
            project = call("POST", "/workspace/projects", PROJECT)
        else:
            sys.exit("project missing; create it first or run with --apply after creation")
    if APPLY:
        call("PATCH", f"/workspace/projects/{project['project_id']}", {
            "artifact_ids": list(artifact_ids.values()),
        })
        print("project attached:", len(artifact_ids), "artifacts")

    for server_name, roots in ROOTS.items():
        print(f"{'[apply]' if APPLY else '[plan] '} roots {server_name}: {roots}")
        if APPLY:
            call("PUT", f"/workspace/server-roots/{server_ids[server_name]}", roots)

    existing_launches = {c["name"] for c in call("GET", "/workspace/launch-configs")}
    for name, working_dir, program, args, required, gpu_count in LAUNCH_CONFIGS:
        if name in existing_launches:
            print(f"= launch config exists: {name}")
            continue
        print(f"{'[apply]' if APPLY else '[plan] '} launch config {name} ({program} {args})")
        if APPLY:
            call("POST", "/workspace/launch-configs", {
                "project_id": project["project_id"],
                "name": name,
                "working_dir": working_dir,
                "program": program,
                "args": args,
                "environment": ENVIRONMENT,
                "required_artifact_ids": [artifact_ids[r] for r in required],
                "gpu_count": gpu_count,
            })

    if APPLY:
        for placement in call("GET", "/workspace/placements"):
            inspection = call(
                "POST", f"/workspace/placements/{placement['placement_id']}/inspect", {}
            )
            print(f"inspect {placement['remote_path'][:58]:60s} -> {inspection['state']}")
    else:
        print("dry-run only; re-run with --apply to write")


if __name__ == "__main__":
    main()

/**
 * Transfer Center Phase 3: resume/incremental/warning surfaces. Covers the
 * normalizer tolerance for the new TransferJob/TransferPlan fields, the
 * transferKind decision, row rendering of the kind chip + resume/skip stats +
 * warnings chip, and the plan preview's space warning / target free line.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import {
  normalizeTransferJob,
  normalizeTransferPlan,
  transfersApi,
} from "../services/transfersApi";
import { transferKind } from "../types/transfers";
import { useConsoleStore } from "../store/consoleStore";
import { useTransferStore } from "../store/transferStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import { TransferJobRow } from "../features/transfers/TransferJobRow";
import { NewTransferDialog } from "../features/transfers/NewTransferDialog";
import type { TransferJob, TransferPlan } from "../types/transfers";
import type { ServerRecord } from "../types/models";
import type { ArtifactRecord, PlacementRecord } from "../types/workspace";

const NOW = "2026-09-17T12:00:00Z";
const GB = 1024 ** 3;

function makeJob(overrides: Partial<TransferJob> = {}): TransferJob {
  return {
    job_id: "j1",
    artifact_id: "a1",
    artifact_label: "DINOv2:b",
    source_server_id: "srv-a",
    source_path: "/data/dinov2",
    target_server_id: "srv-b",
    target_path: "/data/dinov2-mirror",
    strategy_requested: "auto",
    strategy_used: "direct_rsync",
    state: "running",
    excludes: [],
    immutable: false,
    strategy_reason: "direct rsync preflight passed (auto)",
    resumed_bytes: 0,
    files_skipped: 0,
    bytes_skipped: 0,
    warnings: [],
    bytes_total: 10 * GB,
    bytes_done: 2 * GB,
    files_total: 100,
    files_done: 20,
    rate_bps: 80 * 1024 ** 2,
    eta_s: 120,
    current_path: "/data/dinov2/train/images",
    created_at: NOW,
    started_at: NOW,
    finished_at: null,
    error_code: null,
    error_message: "",
    ...overrides,
  };
}

const planFixture: TransferPlan = {
  artifact_id: "a1",
  source_server_id: "srv-a",
  source_path: "/data/dinov2",
  target_server_id: "srv-b",
  target_path: "/data/DINOv2:b",
  strategy_requested: "auto",
  strategy_available: { direct_rsync: true, local_relay: true },
  strategy_selected: "direct_rsync",
  reason: "direct rsync preflight passed (auto)",
  source_exists: true,
  source_size_b: 10 * GB,
  excludes: [],
  target_free_b: null,
  space_warning: "",
};

const servers: ServerRecord[] = [
  { server_id: "srv-a", display_name: "lab-4090", ssh_host: "10.0.0.8", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-b", display_name: "one4090", ssh_host: "10.0.0.9", username: null, port: 22, tags: [], enabled: true },
];

const artifact: ArtifactRecord = {
  artifact_id: "a1",
  kind: "dataset",
  name: "DINOv2",
  version: "b",
  description: "",
  immutable: true,
  created_at: NOW,
  updated_at: NOW,
};

const placement: PlacementRecord = {
  placement_id: "pl1",
  artifact_id: "a1",
  server_id: "srv-a",
  remote_path: "/data/dinov2",
  created_at: NOW,
  updated_at: NOW,
};

function resetStores(): void {
  useTransferStore.setState({
    jobs: [],
    loading: false,
    error: null,
    polling: false,
    dialog: { open: false, prefill: {} },
  });
  useTransferStore.getState().stopPolling();
  useWorkspaceStore.setState({
    artifacts: [],
    artifactsLoading: false,
    artifactsError: null,
    placements: [],
    placementsLoading: false,
    placementsError: null,
    serverRoots: {},
    rootsLoading: {},
    rootsErrors: {},
  });
  useConsoleStore.setState({ servers: [], statuses: {} });
}

describe("transfer normalizers: Phase 3 fields", () => {
  it("reads the new job fields when present", () => {
    const job = normalizeTransferJob({
      job_id: "j1",
      artifact_id: "a1",
      source_server_id: "srv-a",
      source_path: "/data/dinov2",
      target_server_id: "srv-b",
      target_path: "/data/dinov2-mirror",
      immutable: true,
      strategy_reason: "local relay requested",
      resumed_bytes: 74 * GB,
      files_skipped: 12,
      bytes_skipped: 5 * GB,
      warnings: ["skipped symlink: /data/dinov2/link", "skipped symlink: /data/dinov2/other"],
    });
    expect(job).not.toBeNull();
    expect(job?.immutable).toBe(true);
    expect(job?.strategy_reason).toBe("local relay requested");
    expect(job?.resumed_bytes).toBe(74 * GB);
    expect(job?.files_skipped).toBe(12);
    expect(job?.bytes_skipped).toBe(5 * GB);
    expect(job?.warnings).toEqual([
      "skipped symlink: /data/dinov2/link",
      "skipped symlink: /data/dinov2/other",
    ]);
  });

  it("defaults the new job fields for legacy payloads", () => {
    const job = normalizeTransferJob({
      job_id: "j2",
      artifact_id: "a1",
      source_server_id: "srv-a",
      source_path: "/data/dinov2",
      target_server_id: "srv-b",
      target_path: "/data/dinov2-mirror",
    });
    expect(job).not.toBeNull();
    expect(job?.immutable).toBe(false);
    expect(job?.strategy_reason).toBe("");
    expect(job?.resumed_bytes).toBe(0);
    expect(job?.files_skipped).toBe(0);
    expect(job?.bytes_skipped).toBe(0);
    expect(job?.warnings).toEqual([]);
  });

  it("drops non-string entries from warnings", () => {
    const job = normalizeTransferJob({
      job_id: "j3",
      artifact_id: "a1",
      source_server_id: "srv-a",
      source_path: "/x",
      target_server_id: "srv-b",
      target_path: "/y",
      warnings: ["kept", 42, null, "also kept"],
    });
    expect(job?.warnings).toEqual(["kept", "also kept"]);
  });

  it("reads target_free_b and space_warning on plans, defaulting to null and empty", () => {
    const plan = normalizeTransferPlan({
      artifact_id: "a1",
      source_server_id: "srv-a",
      source_path: "/x",
      target_server_id: "srv-b",
      target_path: "/y",
      target_free_b: 12 * GB,
      space_warning: "target has 6.2 GB free, transfer needs 10.0 GB",
    });
    expect(plan).not.toBeNull();
    expect(plan?.target_free_b).toBe(12 * GB);
    expect(plan?.space_warning).toBe("target has 6.2 GB free, transfer needs 10.0 GB");

    const bare = normalizeTransferPlan({
      artifact_id: "a1",
      source_server_id: "srv-a",
      source_path: "/x",
      target_server_id: "srv-b",
      target_path: "/y",
    });
    expect(bare).not.toBeNull();
    expect(bare?.target_free_b).toBeNull();
    expect(bare?.space_warning).toBe("");
  });
});

describe("transferKind", () => {
  it("is fresh when nothing was resumed or skipped", () => {
    expect(transferKind({ resumed_bytes: 0, bytes_skipped: 0 })).toBe("fresh");
  });

  it("is resumed when only a partial was carried over", () => {
    expect(transferKind({ resumed_bytes: 74 * GB, bytes_skipped: 0 })).toBe("resumed");
  });

  it("is incremental when bytes were skipped, subsuming the resumed case", () => {
    expect(transferKind({ resumed_bytes: 0, bytes_skipped: 5 * GB })).toBe("incremental");
    expect(transferKind({ resumed_bytes: 2 * GB, bytes_skipped: 5 * GB })).toBe("incremental");
  });
});

describe("TransferJobRow: Phase 3 surfaces", () => {
  beforeEach(() => {
    useConsoleStore.setState({ servers, statuses: {} });
  });

  afterEach(() => {
    // Unmount before any store resets so updates never hit a mounted tree.
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the resumed chip, resume and skipped-file stats", () => {
    render(
      <TransferJobRow
        job={makeJob({ resumed_bytes: 74 * GB, files_skipped: 12 })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("resumed")).toBeTruthy();
    expect(screen.getByText("Resumed 74.0 GB")).toBeTruthy();
    expect(screen.getByText("Skipped 12 files")).toBeTruthy();
    expect(screen.queryByText("incremental")).toBeNull();
  });

  it("shows the incremental chip for skipped bytes even when resuming", () => {
    render(
      <TransferJobRow
        job={makeJob({ resumed_bytes: 2 * GB, bytes_skipped: 5 * GB })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("incremental")).toBeTruthy();
    expect(screen.queryByText("resumed")).toBeNull();
  });

  it("exposes the planner reason on the strategy chip hover only", () => {
    render(
      <TransferJobRow
        job={makeJob({ strategy_reason: "local relay requested" })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    // The strategy word appears twice by design (chip + flow line); the title
    // lives on the chip only and never becomes visible text.
    const titles = screen
      .getAllByText("Direct rsync")
      .map((el) => el.getAttribute("title"));
    expect(titles).toContain("local relay requested");
    expect(screen.queryByText("local relay requested")).toBeNull();
  });

  it("renders a warnings chip counting entries, first ≤3 on hover", () => {
    render(
      <TransferJobRow
        job={makeJob({
          warnings: [
            "skipped symlink: /data/one",
            "skipped symlink: /data/two",
            "skipped symlink: /data/three",
            "skipped symlink: /data/four",
          ],
        })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    const chip = screen.getByText("4 warnings");
    const title = chip.getAttribute("title") ?? "";
    expect(title).toContain("skipped symlink: /data/one");
    expect(title).toContain("skipped symlink: /data/two");
    expect(title).toContain("skipped symlink: /data/three");
    expect(title).not.toContain("skipped symlink: /data/four");
  });

  it("renders none of the Phase 3 surfaces for a fresh job", () => {
    render(
      <TransferJobRow job={makeJob()} onCancel={() => undefined} onRetry={() => undefined} />,
    );
    expect(screen.queryByText("resumed")).toBeNull();
    expect(screen.queryByText("incremental")).toBeNull();
    expect(screen.queryByText(/Resumed /)).toBeNull();
    expect(screen.queryByText(/Skipped \d+ files/)).toBeNull();
    expect(screen.queryByText(/warnings?$/)).toBeNull();
  });
});

describe("NewTransferDialog: plan space surfaces", () => {
  beforeEach(() => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artifact],
      artifactsLoading: false,
      artifactsError: null,
      placements: [placement],
      placementsLoading: false,
      placementsError: null,
      serverRoots: {
        "srv-b": { project_root: "/proj", dataset_root: "/data", model_root: "/models", output_root: null },
      },
      rootsLoading: {},
      rootsErrors: {},
    });
  });

  afterEach(() => {
    // Unmount before any store resets so updates never hit a mounted tree.
    cleanup();
    resetStores();
    vi.restoreAllMocks();
  });

  function openDialog(): void {
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "a1" } },
    });
    render(<NewTransferDialog open />);
  }

  it("shows the plan's space warning verbatim when present", async () => {
    vi.spyOn(transfersApi, "plan").mockResolvedValue({
      ...planFixture,
      space_warning: "target has 6.2 GB free, transfer needs 10.0 GB",
    });
    openDialog();
    expect(
      await screen.findByText("target has 6.2 GB free, transfer needs 10.0 GB"),
    ).toBeTruthy();
    await waitFor(() => {
      expect(screen.queryByText(/Target free:/)).toBeNull();
    });
  });

  it("shows the quiet target free line when no warning and the probe is known", async () => {
    vi.spyOn(transfersApi, "plan").mockResolvedValue({
      ...planFixture,
      target_free_b: 12 * GB,
    });
    openDialog();
    expect(await screen.findByText("Target free: 12.0 GB")).toBeTruthy();
  });
});

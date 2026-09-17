/**
 * Workspace Phase 4E: the distribution matrix's five states (+ undeclared
 * cells, path titles, placement fallback), the mount-time GET with zero
 * inspect calls, the explicit 检查资源 action, the sync dialog (plan →
 * review → confirm, unresolved blocking, invalid plans, 409 verbatim), the
 * project page's "syncing k/N" line with the one-shot terminal refetch, the
 * Transfer Center's batch groups, the batch-aware polling stop, and the new
 * normalizers' tolerance for missing/unknown fields.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { workspaceApi } from "../services/workspaceApi";
import {
  normalizeProjectDistribution,
  normalizeProjectSyncPlan,
} from "../services/workspaceApi";
import { normalizeTransferBatch, transfersApi } from "../services/transfersApi";
import { ApiError } from "../services/api";
import { useWorkspaceStore } from "../store/workspaceStore";
import { TRANSFER_POLL_MS, useTransferStore } from "../store/transferStore";
import { useConsoleStore } from "../store/consoleStore";
import { en } from "../i18n/en";
import { zh } from "../i18n/zh";
import type { ServerRecord } from "../types/models";
import type {
  ArtifactRecord,
  ArtifactSyncItem,
  DistributionItem,
  PlacementRecord,
  ProjectDistribution,
  ProjectRecord,
  ProjectSyncPlan,
} from "../types/workspace";
import type { TransferBatch, TransferJob } from "../types/transfers";
import { ProjectDetail } from "../features/workspace/ProjectDetail";
import { SyncDialog } from "../features/workspace/SyncDialog";
import { TransfersPage } from "../features/transfers/TransfersPage";

const NOW = "2026-09-17T12:00:00Z";

const servers: ServerRecord[] = [
  { server_id: "srv-a", display_name: "lab-4090", ssh_host: "10.0.0.8", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-b", display_name: "one4090", ssh_host: "10.0.0.9", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-off", display_name: "off", ssh_host: "10.0.0.10", username: null, port: 22, tags: [], enabled: false },
];

function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "p1",
    name: "dinov2-linear",
    description: "",
    artifact_ids: ["a1"],
    launch_config_ids: [],
    tags: [],
    transfer_excludes: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeArtifact(overrides: Partial<ArtifactRecord> = {}): ArtifactRecord {
  return {
    artifact_id: "a1",
    kind: "dataset",
    name: "DINOv2",
    version: "b",
    description: "",
    immutable: true,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makePlacement(overrides: Partial<PlacementRecord> = {}): PlacementRecord {
  return {
    placement_id: "pl1",
    artifact_id: "a1",
    server_id: "srv-a",
    remote_path: "/data/dinov2",
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeItem(overrides: Partial<DistributionItem> = {}): DistributionItem {
  return {
    artifact_id: "a1",
    artifact_label: "DINOv2:b",
    artifact_kind: "dataset",
    server_id: "srv-a",
    placement_id: "pl1",
    remote_path: "/data/dinov2",
    state: "declared",
    checked_at: null,
    detail: null,
    active_transfer_job_id: null,
    ...overrides,
  };
}

function makeDistribution(overrides: Partial<ProjectDistribution> = {}): ProjectDistribution {
  return {
    project_id: "p1",
    generated_at: NOW,
    items: [],
    ...overrides,
  };
}

function makeSyncItem(overrides: Partial<ArtifactSyncItem> = {}): ArtifactSyncItem {
  return {
    artifact_id: "a1",
    artifact_label: "DINOv2:b",
    artifact_kind: "dataset",
    target_status: "missing",
    action: "transfer",
    reason: "",
    source_placement_id: "pl1",
    source_server_id: "srv-a",
    source_path: "/data/dinov2",
    target_path: "/data/DINOv2:b",
    strategy_selected: "direct_rsync",
    alternatives: [],
    warnings: [],
    ...overrides,
  };
}

function makeSyncPlan(overrides: Partial<ProjectSyncPlan> = {}): ProjectSyncPlan {
  return {
    project_id: "p1",
    target_server_id: "srv-b",
    generated_at: NOW,
    refresh_code: false,
    items: [makeSyncItem()],
    valid: true,
    error: "",
    ...overrides,
  };
}

function makeBatch(overrides: Partial<TransferBatch> = {}): TransferBatch {
  return {
    batch_id: "b1",
    project_id: "p1",
    target_server_id: "srv-b",
    job_ids: ["j1"],
    created_at: NOW,
    state: "running",
    total_jobs: 1,
    queued_jobs: 0,
    running_jobs: 1,
    completed_jobs: 0,
    failed_jobs: 0,
    cancelled_jobs: 0,
    ...overrides,
  };
}

function makeJob(overrides: Partial<TransferJob> = {}): TransferJob {
  return {
    job_id: "j1",
    artifact_id: "a1",
    artifact_label: "DINOv2:b",
    source_server_id: "srv-a",
    source_path: "/data/dinov2",
    target_server_id: "srv-b",
    target_path: "/data/DINOv2:b",
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
    bytes_total: null,
    bytes_done: 0,
    files_total: null,
    files_done: 0,
    rate_bps: null,
    eta_s: null,
    current_path: null,
    created_at: NOW,
    started_at: NOW,
    finished_at: null,
    error_code: null,
    error_message: "",
    ...overrides,
  };
}

function resetWorkspaceState(): void {
  useWorkspaceStore.setState({
    projects: [],
    projectsLoading: false,
    projectsError: null,
    artifacts: [],
    artifactsLoading: false,
    artifactsError: null,
    placements: [],
    placementsLoading: false,
    placementsError: null,
    launchConfigs: [],
    launchConfigsLoading: false,
    launchConfigsError: null,
    serverRoots: {},
    rootsLoading: {},
    rootsErrors: {},
    inspections: {},
    inspecting: {},
    inspectErrors: {},
    distributions: {},
    distributionLoading: {},
    distributionErrors: {},
    projectInspecting: {},
    projectInspectErrors: {},
    syncPlan: null,
    syncPlanLoading: false,
    syncPlanError: null,
    syncRunning: false,
    syncError: null,
  });
}

function resetTransferState(): void {
  useTransferStore.setState({
    jobs: [],
    loading: false,
    error: null,
    polling: false,
    batches: [],
    batchesLoading: false,
    batchesError: null,
    dialog: { open: false, prefill: {} },
  });
  useTransferStore.getState().stopPolling();
}

function resetConsole(): void {
  useConsoleStore.setState({ servers: [], statuses: {} });
}

describe("ProjectDetail distribution matrix (Phase 4E)", () => {
  beforeEach(() => {
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      projects: [
        makeProject({
          artifact_ids: ["a1", "a2", "a3"],
        }),
      ],
      artifacts: [
        makeArtifact(),
        makeArtifact({ artifact_id: "a2", kind: "model", name: "SAM", version: null }),
        makeArtifact({ artifact_id: "a3", kind: "code", name: "trainer", version: null }),
      ],
      placements: [makePlacement()],
    });
  });

  afterEach(() => {
    cleanup();
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("renders the five states plus undeclared cells, with path titles", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(
      makeDistribution({
        items: [
          makeItem({ state: "verified", checked_at: NOW }),
          makeItem({ server_id: "srv-b", placement_id: "pl2", state: "declared" }),
          makeItem({ artifact_id: "a2", artifact_label: "SAM", artifact_kind: "model", server_id: "srv-a", placement_id: "pl3", remote_path: "/models/sam", state: "missing" }),
          makeItem({ artifact_id: "a2", artifact_label: "SAM", artifact_kind: "model", server_id: "srv-b", placement_id: "pl4", remote_path: "/models/sam2", state: "unavailable", detail: "ssh failed" }),
          makeItem({ artifact_id: "a3", artifact_label: "trainer", artifact_kind: "code", placement_id: "pl5", remote_path: "/code/trainer", state: "syncing", active_transfer_job_id: "j9" }),
        ],
      }),
    );

    render(<ProjectDetail projectId="p1" />);

    expect(await screen.findByLabelText("present")).toBeTruthy();
    expect(screen.getAllByLabelText("present")).toHaveLength(1);
    expect(screen.getAllByLabelText("declared")).toHaveLength(1);
    expect(screen.getAllByLabelText("missing")).toHaveLength(1);
    expect(screen.getAllByLabelText("unavailable")).toHaveLength(1);
    expect(screen.getAllByLabelText("syncing")).toHaveLength(1);
    // No item and no placement: a1/a2/a3 on the disabled srv-off column and
    // a3 on srv-b render the no-declaration dash.
    expect(screen.getAllByLabelText("undeclared")).toHaveLength(4);

    expect(screen.getByLabelText("present").textContent).toBe("✓");
    expect(screen.getByLabelText("declared").textContent).toBe("○");
    expect(screen.getByLabelText("missing").textContent).toBe("—");
    expect(screen.getByLabelText("unavailable").textContent).toBe("!");
    expect(screen.getByLabelText("syncing").textContent).toBe("↻");
    expect(
      screen.getAllByLabelText("undeclared").every((node) => node.textContent === "—"),
    ).toBe(true);

    // Titles carry the remote path (plus checked time / detail when present).
    expect(screen.getByLabelText("present").getAttribute("title")).toContain("/data/dinov2");
    expect(screen.getByLabelText("missing").getAttribute("title")).toContain("/models/sam");
    expect(screen.getByLabelText("unavailable").getAttribute("title")).toContain("ssh failed");
    expect(screen.getAllByLabelText("undeclared")[0]?.getAttribute("title")).toBeNull();
  });

  it("falls back to declared placements when the snapshot has no item", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());

    render(<ProjectDetail projectId="p1" />);

    expect(await screen.findByLabelText("declared")).toBeTruthy();
    expect(screen.getAllByLabelText("declared")).toHaveLength(1); // a1@srv-a via placement
    expect(screen.getAllByLabelText("undeclared")).toHaveLength(8); // every other cell
    expect(screen.getByLabelText("declared").getAttribute("title")).toContain("/data/dinov2");
  });

  it("fetches the distribution on mount and never inspects", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    const distSpy = vi
      .spyOn(workspaceApi, "getProjectDistribution")
      .mockResolvedValue(makeDistribution());
    const inspectSpy = vi
      .spyOn(workspaceApi, "inspectProject")
      .mockResolvedValue(makeDistribution());

    render(<ProjectDetail projectId="p1" />);

    await waitFor(() => expect(distSpy).toHaveBeenCalledWith("p1"));
    expect(inspectSpy).not.toHaveBeenCalled();
  });

  it("re-checks resources via the explicit inspect action and refreshes the matrix", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    const distSpy = vi
      .spyOn(workspaceApi, "getProjectDistribution")
      .mockResolvedValue(makeDistribution({ items: [makeItem({ state: "declared" })] }));
    const inspectSpy = vi.spyOn(workspaceApi, "inspectProject").mockResolvedValue(
      makeDistribution({ items: [makeItem({ state: "verified", checked_at: NOW })] }),
    );

    render(<ProjectDetail projectId="p1" />);
    expect(await screen.findByLabelText("declared")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Check resources" }));

    expect(await screen.findByLabelText("present")).toBeTruthy();
    expect(inspectSpy).toHaveBeenCalledTimes(1);
    expect(distSpy).toHaveBeenCalledTimes(1); // the inspect response replaced the cache
  });

  it("shows a light syncing k/N line and refetches the distribution once at terminal", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    const distSpy = vi
      .spyOn(workspaceApi, "getProjectDistribution")
      .mockResolvedValue(makeDistribution());

    render(<ProjectDetail projectId="p1" />);
    await waitFor(() => expect(distSpy).toHaveBeenCalledTimes(1));
    expect(screen.queryByText("Syncing 1/3")).toBeNull();

    act(() => {
      useTransferStore.setState({
        batches: [makeBatch({ state: "running", total_jobs: 3, completed_jobs: 1, running_jobs: 2 })],
      });
    });
    expect(screen.getByText("Syncing 1/3")).toBeTruthy();

    act(() => {
      useTransferStore.setState({
        batches: [makeBatch({ state: "completed", total_jobs: 3, completed_jobs: 3, running_jobs: 0 })],
      });
    });
    await waitFor(() => expect(distSpy).toHaveBeenCalledTimes(2));
    expect(screen.queryByText("Syncing 1/3")).toBeNull();
  });
});

describe("SyncDialog", () => {
  beforeEach(() => {
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({ projects: [makeProject()] });
  });

  afterEach(() => {
    cleanup();
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("builds the plan for the chosen target and renders the decision table", async () => {
    const planSpy = vi
      .spyOn(workspaceApi, "buildSyncPlan")
      .mockImplementation(async (_projectId, body) => {
        expect(body.target_server_id).toBe("srv-b");
        return makeSyncPlan({
          items: [
            makeSyncItem({
              action: "transfer",
              target_status: "missing",
              warnings: ["symlink skipped: latest"],
            }),
            makeSyncItem({
              artifact_id: "a2",
              artifact_label: "SAM",
              artifact_kind: "model",
              action: "skip",
              target_status: "verified",
              source_placement_id: null,
              source_server_id: null,
              source_path: null,
              target_path: null,
              strategy_selected: null,
            }),
            makeSyncItem({
              artifact_id: "a3",
              artifact_label: "trainer",
              artifact_kind: "code",
              action: "unresolved",
              target_status: "declared",
              reason: "no source placement available",
              source_placement_id: null,
              source_server_id: null,
              source_path: null,
              target_path: null,
              strategy_selected: null,
            }),
          ],
        });
      });
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());

    render(<SyncDialog open onClose={() => undefined} projectId="p1" />);

    fireEvent.change(screen.getByLabelText("Target server"), {
      target: { value: "srv-b" },
    });
    await waitFor(() => expect(planSpy).toHaveBeenCalledTimes(1));

    expect(await screen.findByText("DINOv2:b")).toBeTruthy();
    expect(screen.getByText("missing")).toBeTruthy(); // Current column, transfer row
    expect(screen.getByText("verified")).toBeTruthy(); // Current column, skip row

    // TRANSFER row expands to source → target, strategy and warnings.
    fireEvent.click(screen.getByRole("button", { name: "Transfer details for DINOv2:b" }));
    expect(await screen.findByText("lab-4090:/data/dinov2 → /data/DINOv2:b")).toBeTruthy();
    expect(screen.getByText("Strategy: Direct rsync")).toBeTruthy();
    expect(screen.getByText("symlink skipped: latest")).toBeTruthy();

    // UNRESOLVED shows the backend reason in place and blocks the confirm.
    expect(screen.getByText("no source placement available")).toBeTruthy();
    expect(screen.getByText("Unresolved items block the sync — resolve them first.")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Sync 1 resources" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it("disables confirm and shows the plan error when the plan is invalid", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());
    vi.spyOn(workspaceApi, "buildSyncPlan").mockResolvedValue(
      makeSyncPlan({ valid: false, error: "overlapping TRANSFER target paths" }),
    );

    render(<SyncDialog open onClose={() => undefined} projectId="p1" />);
    fireEvent.change(screen.getByLabelText("Target server"), {
      target: { value: "srv-b" },
    });

    expect(await screen.findByText("overlapping TRANSFER target paths")).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Sync 1 resources" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it("offers no confirm when every item is skipped", async () => {
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());
    vi.spyOn(workspaceApi, "buildSyncPlan").mockResolvedValue(
      makeSyncPlan({ items: [makeSyncItem({ action: "skip", target_status: "verified" })] }),
    );

    render(<SyncDialog open onClose={() => undefined} projectId="p1" />);
    fireEvent.change(screen.getByLabelText("Target server"), {
      target: { value: "srv-b" },
    });

    const confirm = (await screen.findByText("No missing resources"))
      .closest("button") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it("confirms the sync, closes the dialog and surfaces the batch", async () => {
    vi.spyOn(workspaceApi, "syncProject").mockImplementation(async (_projectId, body) => {
      expect(body.target_server_id).toBe("srv-b");
      return makeBatch();
    });
    const listBatchesSpy = vi.spyOn(transfersApi, "listBatches").mockResolvedValue([makeBatch()]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());
    vi.spyOn(workspaceApi, "buildSyncPlan").mockResolvedValue(makeSyncPlan());
    const onClose = vi.fn();

    render(<SyncDialog open onClose={onClose} projectId="p1" />);
    fireEvent.change(screen.getByLabelText("Target server"), {
      target: { value: "srv-b" },
    });

    const confirm = (await screen.findByRole("button", {
      name: "Sync 1 resources",
    })) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(false));
    fireEvent.click(confirm);

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listBatchesSpy).toHaveBeenCalled());
  });

  it("keeps the dialog open and shows the 409 rejection verbatim", async () => {
    vi.spyOn(workspaceApi, "syncProject").mockRejectedValue(
      new ApiError(409, "sync refused: 1 unresolved item"),
    );
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(makeDistribution());
    vi.spyOn(workspaceApi, "buildSyncPlan").mockResolvedValue(makeSyncPlan());
    const onClose = vi.fn();

    render(<SyncDialog open onClose={onClose} projectId="p1" />);
    fireEvent.change(screen.getByLabelText("Target server"), {
      target: { value: "srv-b" },
    });

    const confirm = (await screen.findByRole("button", {
      name: "Sync 1 resources",
    })) as HTMLButtonElement;
    await waitFor(() => expect(confirm.disabled).toBe(false));
    fireEvent.click(confirm);

    expect(await screen.findByText("sync refused: 1 unresolved item")).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe("TransfersPage batch groups", () => {
  beforeEach(() => {
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({ projects: [makeProject()] });
    vi.spyOn(workspaceApi, "listArtifacts").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "listPlacements").mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    resetWorkspaceState();
    resetTransferState();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("groups batch jobs under a collapsible header and keeps unbatched jobs flat", async () => {
    vi.spyOn(transfersApi, "listJobs").mockResolvedValue([
      makeJob(),
      makeJob({ job_id: "j2", state: "completed" }),
      makeJob({ job_id: "j3", state: "failed", error_message: "boom" }),
      makeJob({ job_id: "j9", artifact_label: "SAM" }),
    ]);
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([
      makeBatch({
        job_ids: ["j1", "j2", "j3"],
        total_jobs: 3,
        completed_jobs: 1,
        running_jobs: 1,
        failed_jobs: 1,
      }),
    ]);

    render(<TransfersPage />);

    expect(await screen.findByText(/dinov2-linear → one4090/)).toBeTruthy();
    expect(screen.getByText(/1\/3 completed/)).toBeTruthy();
    expect(screen.getByText(/1 running/)).toBeTruthy();
    expect(screen.getByText(/1 failed/)).toBeTruthy();
    // Active batches start expanded: the batch's jobs reuse TransferJobRow.
    expect(screen.getAllByText("DINOv2:b")).toHaveLength(3);
    // Jobs outside the batch stay in the flat list.
    expect(screen.getByText("SAM")).toBeTruthy();

    const toggle = screen.getByRole("button", { name: /dinov2-linear → one4090/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(screen.queryByText("DINOv2:b")).toBeNull();
    fireEvent.click(toggle);
    expect((await screen.findAllByText("DINOv2:b")).length).toBe(3);
  });

  it("stops polling once jobs and batches are all terminal", async () => {
    vi.useFakeTimers();
    try {
      const store = useTransferStore;
      const listJobs = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([]);
      const listBatches = vi
        .spyOn(transfersApi, "listBatches")
        .mockResolvedValue([makeBatch({ total_jobs: 1, running_jobs: 1 })]);

      await store.getState().fetchBatches();
      expect(store.getState().polling).toBe(true);
      expect(listJobs).not.toHaveBeenCalled();

      listBatches.mockResolvedValue([
        makeBatch({ state: "completed", total_jobs: 1, completed_jobs: 1, running_jobs: 0 }),
      ]);
      await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS); // tick refreshes jobs + batches
      expect(store.getState().polling).toBe(false);

      const after = listJobs.mock.calls.length;
      await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS * 3);
      expect(listJobs.mock.calls.length).toBe(after);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Phase 4E normalizers", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("parses the distribution snapshot with defaults for missing fields", () => {
    const distribution = normalizeProjectDistribution({
      project_id: "p1",
      generated_at: NOW,
      items: [
        {
          artifact_id: "a1",
          artifact_label: "DINOv2:b",
          artifact_kind: "dataset",
          server_id: "srv-a",
          placement_id: "pl1",
          remote_path: "/data/dinov2",
          state: "verified",
          checked_at: NOW,
          detail: "ok",
          active_transfer_job_id: null,
        },
        { artifact_id: "a2", server_id: "srv-a", remote_path: "/x", state: "bogus" },
        "junk",
      ],
    });
    expect(distribution).not.toBeNull();
    expect(distribution?.items).toHaveLength(2);
    expect(distribution?.items[0]?.state).toBe("verified");
    expect(distribution?.items[0]?.detail).toBe("ok");
    // Unknown state and missing strings degrade to safe defaults.
    expect(distribution?.items[1]?.state).toBe("declared");
    expect(distribution?.items[1]?.artifact_label).toBe("");
    expect(distribution?.items[1]?.checked_at).toBeNull();

    expect(normalizeProjectDistribution({ project_id: "p1" })?.generated_at).toBe("");
    expect(normalizeProjectDistribution({ items: [] })).toBeNull();
    expect(normalizeProjectDistribution(null)).toBeNull();
  });

  it("parses sync plan items; unknown actions degrade to unresolved", () => {
    const plan = normalizeProjectSyncPlan({
      project_id: "p1",
      target_server_id: "srv-b",
      generated_at: NOW,
      refresh_code: false,
      valid: true,
      items: [
        {
          artifact_id: "a1",
          artifact_label: "DINOv2:b",
          artifact_kind: "dataset",
          target_status: "missing",
          action: "transfer",
          reason: "",
          source_server_id: "srv-a",
          source_path: "/s",
          target_path: "/t",
          strategy_selected: "direct_rsync",
          alternatives: ["alt"],
          warnings: ["w1"],
        },
        { artifact_id: "a2", action: "weird" },
      ],
    });
    expect(plan).not.toBeNull();
    expect(plan?.items[0]?.strategy_selected).toBe("direct_rsync");
    expect(plan?.items[0]?.source_placement_id).toBeNull();
    expect(plan?.items[0]?.alternatives).toEqual(["alt"]);
    expect(plan?.items[1]?.action).toBe("unresolved");
    expect(plan?.items[1]?.strategy_selected).toBeNull();
    expect(plan?.valid).toBe(true);

    const invalid = normalizeProjectSyncPlan({
      project_id: "p1",
      target_server_id: "srv-b",
      valid: false,
      error: "boom",
    });
    expect(invalid?.valid).toBe(false);
    expect(invalid?.error).toBe("boom");
    expect(normalizeProjectSyncPlan("junk")).toBeNull();
    expect(normalizeProjectSyncPlan({ project_id: "p1" })).toBeNull();
  });

  it("parses transfer batches with count defaults", () => {
    const batch = normalizeTransferBatch({
      batch_id: "b1",
      project_id: "p1",
      target_server_id: "srv-b",
      job_ids: ["j1", "j2"],
      created_at: NOW,
      state: "partial_failed",
      total_jobs: 2,
      completed_jobs: 1,
      failed_jobs: 1,
    });
    expect(batch).toEqual({
      batch_id: "b1",
      project_id: "p1",
      target_server_id: "srv-b",
      job_ids: ["j1", "j2"],
      created_at: NOW,
      state: "partial_failed",
      total_jobs: 2,
      queued_jobs: 0,
      running_jobs: 0,
      completed_jobs: 1,
      failed_jobs: 1,
      cancelled_jobs: 0,
    });

    const tolerant = normalizeTransferBatch({
      batch_id: "b2",
      project_id: "p1",
      target_server_id: "srv-b",
      state: "weird",
      total_jobs: "x",
    });
    expect(tolerant?.state).toBe("queued");
    expect(tolerant?.total_jobs).toBe(0);
    expect(tolerant?.job_ids).toEqual([]);
    expect(normalizeTransferBatch(null)).toBeNull();
    expect(normalizeTransferBatch({ project_id: "p1" })).toBeNull();
  });
});

describe("Phase 4E i18n parity", () => {
  it("zh mirrors the new workspace and transfers keys", () => {
    for (const key of [
      "inspectAll",
      "syncMissing",
      "syncProgress",
      "stateSyncing",
      "syncDialogTitle",
      "syncPlanning",
      "syncCurrent",
      "syncAction",
      "syncActionSkip",
      "syncActionTransfer",
      "syncActionUnresolved",
      "syncUnresolvedHint",
      "syncStrategy",
      "syncDetailAria",
      "syncConfirm",
      "syncNothing",
      "matrixUndeclared",
    ] as const) {
      expect(typeof en.workspace[key]).toBe("string");
      expect(typeof zh.workspace[key]).toBe("string");
    }
    for (const key of ["batchDone", "batchRunningCount", "batchFailedCount"] as const) {
      expect(typeof en.transfers[key]).toBe("string");
      expect(typeof zh.transfers[key]).toBe("string");
    }
    expect(zh.workspace.syncConfirm).toBe("同步 {n} 项资源");
    expect(zh.workspace.syncProgress).toBe("正在同步 {done}/{total}");
    expect(zh.workspace.syncUnresolvedHint).toContain("请先处理");
    expect(zh.transfers.batchDone).toBe("{done}/{total} 已完成");
  });
});

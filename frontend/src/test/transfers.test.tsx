/**
 * Transfer Center (Phase 2): the #/transfers hash route, the 1 Hz polling
 * law (armed only while active jobs exist, never duplicated, cleared when
 * none remain and on unmount), job row rendering (bytes/progress/strategy/
 * rate/ETA/actions), the new transfer dialog (prefill + payload), cancel/
 * retry calls, and the zh/en transfers dictionary parity.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { parseHash, routeToHash } from "../shell/routes";
import { transfersApi } from "../services/transfersApi";
import { workspaceApi } from "../services/workspaceApi";
import { TRANSFER_POLL_MS, useTransferStore } from "../store/transferStore";
import { useConsoleStore } from "../store/consoleStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import { en } from "../i18n/en";
import { zh } from "../i18n/zh";
import type { TransferJob, TransferPlan } from "../types/transfers";
import type { ServerRecord } from "../types/models";
import type { ArtifactRecord, PlacementRecord } from "../types/workspace";
import { TransfersPage } from "../features/transfers/TransfersPage";
import { TransferJobRow } from "../features/transfers/TransferJobRow";
import { NewTransferDialog } from "../features/transfers/NewTransferDialog";

const NOW = "2026-09-17T12:00:00Z";

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
    bytes_total: 10 * 1024 ** 3,
    bytes_done: 2 * 1024 ** 3,
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
  source_size_b: 10 * 1024 ** 3,
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
    batches: [],
    batchesLoading: false,
    batchesError: null,
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
  window.location.hash = "";
}

describe("transfers hash route", () => {
  it("parses #/transfers and round-trips", () => {
    expect(parseHash("#/transfers")).toEqual({ name: "transfers" });
    expect(parseHash(routeToHash({ name: "transfers" }))).toEqual({ name: "transfers" });
    expect(parseHash("#/transfers/extra")).toEqual({ name: "transfers" });
  });
});

describe("polling law", () => {
  beforeEach(() => {
    useTransferStore.getState().stopPolling();
  });

  afterEach(() => {
    useTransferStore.getState().stopPolling();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("polls at 1 Hz while a job is active, stops when none remain", async () => {
    vi.useFakeTimers();
    const store = useTransferStore;
    const spy = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([makeJob()]);
    await store.getState().loadJobs();
    expect(store.getState().polling).toBe(true);
    expect(spy).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS);
    expect(spy).toHaveBeenCalledTimes(2);

    // Terminal-only listing clears the timer on the refresh that follows.
    spy.mockResolvedValue([makeJob({ state: "completed" })]);
    await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS);
    expect(store.getState().polling).toBe(false);

    const after = spy.mock.calls.length;
    await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS * 3);
    expect(spy.mock.calls.length).toBe(after);
  });

  it("never duplicates the timer across re-arms", async () => {
    vi.useFakeTimers();
    const store = useTransferStore;
    const spy = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([makeJob()]);
    await store.getState().loadJobs();
    store.getState().syncPolling();
    store.getState().syncPolling();
    await vi.advanceTimersByTimeAsync(TRANSFER_POLL_MS);
    expect(spy).toHaveBeenCalledTimes(2); // initial load + exactly one tick
  });
});

describe("TransferJobRow rendering", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders bytes progress, rate, ETA, strategy and the Cancel verb", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    render(
      <TransferJobRow job={makeJob()} onCancel={() => undefined} onRetry={() => undefined} />,
    );
    expect(screen.getByText("DINOv2:b")).toBeTruthy();
    expect(screen.getByText("2.0/10.0 GB")).toBeTruthy();
    expect(screen.getByText("80.0 MB/s")).toBeTruthy();
    expect(screen.getByText("~2 min left")).toBeTruthy();
    // Strategy word appears twice by design (chip + flow line).
    expect(screen.getAllByText("Direct rsync").length).toBe(2);
    expect(screen.getByText("lab-4090")).toBeTruthy();
    expect(screen.getByText("one4090")).toBeTruthy();
    expect(screen.getByText("/data/dinov2/train/images")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Cancel transfer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Retry transfer" })).toBeNull();
  });

  it("shows the backend error message and Retry on failed rows", () => {
    render(
      <TransferJobRow
        job={makeJob({ state: "failed", error_message: "rsync exited with code 23" })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("rsync exited with code 23")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry transfer" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel transfer" })).toBeNull();
  });

  it("falls back to file counts when bytes_total is unknown", () => {
    render(
      <TransferJobRow
        job={makeJob({ bytes_total: null })}
        onCancel={() => undefined}
        onRetry={() => undefined}
      />,
    );
    expect(screen.getByText("20 / 100 files")).toBeTruthy();
  });
});

describe("TransfersPage", () => {
  beforeEach(() => {
    // The page prefetches the workspace catalog so the New-Transfer dialog
    // works after a direct landing; keep tests hermetic.
    vi.spyOn(workspaceApi, "listArtifacts").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "listPlacements").mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    useTransferStore.getState().stopPolling();
    vi.restoreAllMocks();
  });

  it("renders rows and clears polling on unmount", async () => {
    vi.spyOn(transfersApi, "listJobs").mockResolvedValue([makeJob()]);
    const { unmount } = render(<TransfersPage />);
    expect(await screen.findByText("DINOv2:b")).toBeTruthy();
    expect(useTransferStore.getState().polling).toBe(true);
    unmount();
    expect(useTransferStore.getState().polling).toBe(false);
  });

  it("prefetches workspace assets when landing directly on the page", async () => {
    const listJobs = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([]);
    const listArtifacts = workspaceApi.listArtifacts as ReturnType<typeof vi.fn>;
    const listPlacements = workspaceApi.listPlacements as ReturnType<typeof vi.fn>;
    render(<TransfersPage />);
    await waitFor(() => {
      expect(listJobs).toHaveBeenCalled();
      expect(listArtifacts).toHaveBeenCalled();
      expect(listPlacements).toHaveBeenCalled();
    });
  });

  it("shows the mono header counts for mixed states", async () => {
    vi.spyOn(transfersApi, "listJobs").mockResolvedValue([
      makeJob(),
      makeJob({ job_id: "j2", state: "queued" }),
      makeJob({ job_id: "j3", state: "completed" }),
      makeJob({ job_id: "j4", state: "failed", error_message: "boom" }),
    ]);
    render(<TransfersPage />);
    expect(await screen.findByText("1 active · 1 waiting · 1 completed")).toBeTruthy();
  });

  it("disables clear-history while only active jobs exist", async () => {
    resetStores();
    vi.spyOn(transfersApi, "listJobs").mockResolvedValue([makeJob()]);
    render(<TransfersPage />);
    await screen.findByText("DINOv2:b");
    const button = screen.getByRole("button", { name: "Clear history" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    // No destructive dialog is reachable from a disabled control.
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("clears terminal history through the named confirm dialog", async () => {
    resetStores();
    const listSpy = vi
      .spyOn(transfersApi, "listJobs")
      .mockResolvedValue([makeJob({ state: "completed" })]);
    const clearSpy = vi.spyOn(transfersApi, "clearHistory").mockResolvedValue(1);
    render(<TransfersPage />);
    await screen.findByText("DINOv2:b");

    const button = screen.getByRole("button", { name: "Clear history" }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);

    // Named confirm dialog (never window.confirm): localized title + body.
    const dialog = await screen.findByRole("dialog", { name: "Clear transfer history?" });
    expect(
      within(dialog).getByText(/Removes completed, failed and cancelled jobs/),
    ).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "Clear history" }));
    await waitFor(() => expect(clearSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(listSpy.mock.calls.length).toBe(2)); // initial load + refresh
  });
});

describe("NewTransferDialog", () => {
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
    cleanup();
    resetStores();
    vi.restoreAllMocks();
  });

  it("prefills from the 同步到… intent and posts the TransferRequest payload", async () => {
    const planSpy = vi.spyOn(transfersApi, "plan").mockResolvedValue(planFixture);
    const createSpy = vi.spyOn(transfersApi, "create").mockResolvedValue("j9");
    const listSpy = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([]);
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "a1", sourcePlacementId: "pl1" } },
    });

    render(<NewTransferDialog open />);

    // Prefilled artifact + source placement, target server defaults to the
    // first enabled non-source server, and the path is suggested from the
    // server's dataset root + name:version.
    expect(
      (screen.getByLabelText("Source placement") as HTMLSelectElement).value,
    ).toBe("pl1");
    await screen.findByDisplayValue("/data/DINOv2:b");
    await waitFor(() => expect(planSpy).toHaveBeenCalled()); // debounced plan

    fireEvent.click(screen.getByRole("button", { name: "Start transfer" }));

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        artifact_id: "a1",
        source_placement_id: "pl1",
        target_server_id: "srv-b",
        target_path: "/data/DINOv2:b",
        strategy: "auto",
        verify_mode: "quick",
      });
    });
    expect(listSpy).toHaveBeenCalled(); // list refreshed after create
    expect(useTransferStore.getState().dialog.open).toBe(false);
  });

  it("shows the effective project exclude patterns in the plan preview", async () => {
    vi.spyOn(transfersApi, "plan").mockResolvedValue({
      ...planFixture,
      excludes: ["dataset", "checkpoints"],
    });
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "a1" } },
    });

    render(<NewTransferDialog open />);
    await screen.findByDisplayValue("/data/DINOv2:b");

    expect(await screen.findByText("Skipped by project excludes:")).toBeTruthy();
    expect(screen.getByText("dataset")).toBeTruthy();
    expect(screen.getByText("checkpoints")).toBeTruthy();
  });

  it("shows per-method availability and the localized no-direct-SSH reason", async () => {
    vi.spyOn(transfersApi, "plan").mockResolvedValue({
      ...planFixture,
      strategy_available: { direct_rsync: false, local_relay: true },
      strategy_selected: "local_relay",
      reason: "direct rsync unavailable; falling back to local relay",
    });
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "a1" } },
    });

    render(<NewTransferDialog open />);
    await screen.findByDisplayValue("/data/DINOv2:b");

    await waitFor(() => {
      expect(screen.getAllByText("unavailable").length).toBeGreaterThan(0);
    });
    expect(
      screen.getByText("Source server cannot SSH to the target directly"),
    ).toBeTruthy();
  });
});

describe("cancel and retry calls", () => {
  afterEach(() => {
    useTransferStore.getState().stopPolling();
    vi.restoreAllMocks();
  });

  it("hit the API with the job id and refresh the list", async () => {
    const cancelSpy = vi.spyOn(transfersApi, "cancel").mockResolvedValue(undefined);
    const retrySpy = vi.spyOn(transfersApi, "retry").mockResolvedValue("j2");
    const listSpy = vi.spyOn(transfersApi, "listJobs").mockResolvedValue([]);
    const store = useTransferStore;

    await store.getState().cancelJob("j1");
    expect(cancelSpy).toHaveBeenCalledWith("j1");
    expect(listSpy).toHaveBeenCalled();

    await store.getState().retryJob("j2");
    expect(retrySpy).toHaveBeenCalledWith("j2");
  });
});

describe("transfers i18n parity", () => {
  it("zh mirrors every en transfers key and carries the contract wording", () => {
    expect(Object.keys(zh.transfers).sort()).toEqual(Object.keys(en.transfers).sort());
    for (const key of Object.keys(en.transfers)) {
      expect(typeof zh.transfers[key as keyof typeof en.transfers]).toBe("string");
      expect(typeof en.transfers[key as keyof typeof en.transfers]).toBe("string");
    }
    expect(en.nav.transfers).toBe("Transfers");
    expect(zh.nav.transfers).toBe("传输");
    expect(zh.transfers.title).toBe("传输");
    expect(zh.transfers.newTransfer).toBe("新建传输");
    expect(zh.transfers.strategyAuto).toBe("自动");
    expect(zh.transfers.strategyDirect).toBe("直接同步");
    expect(zh.transfers.strategyRelay).toBe("本机中转");
    expect(zh.transfers.unavailable).toBe("不可用");
    expect(zh.transfers.noDirectSsh).toBe("源服务器无法直接 SSH 到目标服务器");
    expect(zh.transfers.stateCompleted).toBe("传输完成");
    expect(zh.transfers.etaMinutes).toBe("剩余约 {n} 分钟");
    expect(zh.transfers.cancelTransfer).toBe("取消传输");
    expect(zh.transfers.retryTransfer).toBe("重试传输");
    expect(en.transfers.clearHistory).toBe("Clear history");
    expect(zh.transfers.clearHistory).toBe("清空传输历史");
    expect(zh.transfers.clearHistoryTitle).toBe("清空传输历史？");
    expect(zh.transfers.clearHistoryBody).toBe(
      "将清除已完成、失败、已取消的传输记录；进行中的传输与已登记的资产、放置均不受影响。",
    );
    expect(zh.transfers.clearHistoryConfirm).toBe("清空记录");
  });
});

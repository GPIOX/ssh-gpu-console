/**
 * ServerPage (F3) tests over fixture data. The api module is mocked so
 * terminate/kill actions and snapshot refreshes are observable; the singleton
 * store is seeded per test (mirrors what the store does after a live frame).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError, api } from "../services/api";
import { fixtureServers, fixtureSnapshots } from "../data/fixtures";
import { setLocale } from "../i18n";
import { useConsoleStore } from "../store/consoleStore";
import type { ProcessInfo, ServerSnapshot } from "../types/models";
import type { ServerTab } from "../shell/routes";
import { ServerPage } from "../features/server/ServerPage";

vi.mock("../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      terminateProcess: vi.fn(async (_serverId: string, _pid: number) => undefined),
      killProcess: vi.fn(async (_serverId: string, _pid: number) => undefined),
      getSnapshot: vi.fn(async (_serverId: string) => undefined as unknown as ServerSnapshot),
    },
  };
});

const LAB = "srv-lab-4090";

function seedServer(serverId: string, tab: ServerTab): void {
  const snapshot = fixtureSnapshots[serverId];
  useConsoleStore.setState({
    route: { name: "server", serverId, tab },
    selectedServerId: serverId,
    selectedTab: tab,
    servers: fixtureServers,
    snapshots: { [serverId]: snapshot },
    snapshotErrors: {},
    statuses: { [serverId]: snapshot.status },
    history: new Map(),
  });
}

function seedSnapshot(snapshot: ServerSnapshot): void {
  useConsoleStore.setState({
    route: { name: "server", serverId: snapshot.server_id, tab: "overview" },
    selectedServerId: snapshot.server_id,
    selectedTab: "overview",
    servers: fixtureServers,
    snapshots: { [snapshot.server_id]: snapshot },
    snapshotErrors: {},
    statuses: { [snapshot.server_id]: snapshot.status },
    history: new Map(),
  });
}

function firstRowPid(): string {
  return document.querySelector(".srv-table tbody tr td")?.textContent ?? "";
}

function rowPids(): string[] {
  return [...document.querySelectorAll(".srv-table tbody tr")].map(
    (row) => row.querySelector("td")?.textContent ?? "",
  );
}

beforeEach(() => {
  setLocale("en"); // assertions below use the English dictionary
  vi.clearAllMocks();
  useConsoleStore.setState({
    route: { name: "fleet" },
    selectedServerId: null,
    selectedTab: "overview",
    servers: [],
    snapshots: {},
    snapshotErrors: {},
    statuses: {},
    history: new Map(),
  });
});

describe("ServerPage — overview", () => {
  it("renders GPU lanes, the pressure band, network and top processes", () => {
    seedServer(LAB, "overview");
    render(<ServerPage serverId={LAB} />);

    // hero: one lane per GPU with owners correlated from gpu_processes
    expect(document.querySelectorAll(".gpu-lane")).toHaveLength(2);
    expect(screen.getByText("GPU0")).toBeTruthy();
    expect(screen.getByText("GPU1")).toBeTruthy();
    expect(screen.getByText("users: demo")).toBeTruthy();

    // CPU/RAM pressure band with large mono numerals + load averages
    const numerals = [...document.querySelectorAll(".srv-pressure__numeral")];
    expect(numerals.map((node) => node.textContent)).toEqual(["23%", "41%"]);
    expect(screen.getByText("load 2.14 1.98 1.76 · 32 cores")).toBeTruthy();

    // network RX/TX via formatBps
    expect(screen.getByText("RX 1.2 MB/s")).toBeTruthy();
    expect(screen.getByText("TX 243.0 KB/s")).toBeTruthy();

    // top processes: first five by cpu_percent, not a table
    expect(screen.getByText("python")).toBeTruthy();
    expect(document.querySelectorAll(".srv-topprocs__row")).toHaveLength(5);
    expect(document.querySelector(".srv-table")).toBeNull();
  });

  it("renders the storage warning for the highest-percent mount only under pressure", () => {
    seedServer(LAB, "overview");
    render(<ServerPage serverId={LAB} />);

    // /data is at 82% (warn threshold 80) — the section features the worst mount
    expect(screen.getByText("Storage")).toBeTruthy();
    expect(screen.getByText("/data")).toBeTruthy();
    expect(screen.getByText("82% used")).toBeTruthy();
    expect(screen.queryByText("/home")).toBeNull();
  });

  it("renders a quiet empty state (not an error) when no NVIDIA stack exists", () => {
    seedSnapshot({
      ...fixtureSnapshots[LAB],
      gpus: [],
      gpu_processes: [],
      errors: { gpu: "unavailable" },
    });
    render(<ServerPage serverId={LAB} />);

    expect(screen.getByText("No NVIDIA GPU detected")).toBeTruthy();
    expect(document.querySelector(".error-panel")).toBeNull();
  });

  it("desaturates values and shows the stale chip when the snapshot is stale", () => {
    // generated_at is aged so the localized freshness ladder reads "Ns ago"
    seedSnapshot({
      ...fixtureSnapshots[LAB],
      stale: true,
      generated_at: new Date(Date.now() - 30_000).toISOString(),
    });
    render(<ServerPage serverId={LAB} />);

    expect(document.querySelector(".srv-body--stale")).toBeTruthy();
    expect(screen.getByText(/^stale · \d+s ago$/)).toBeTruthy();
  });
});

describe("ServerPage — states", () => {
  it("shows a skeleton while the first snapshot loads", () => {
    useConsoleStore.setState({
      route: { name: "server", serverId: "srv-unknown", tab: "overview" },
      selectedServerId: "srv-unknown",
      servers: [],
      snapshots: {},
      snapshotErrors: {},
      statuses: {},
    });
    render(<ServerPage serverId="srv-unknown" />);
    expect(document.querySelector(".skeleton")).toBeTruthy();
  });

  it("preserves identity on fetch error and retries through the store", async () => {
    vi.mocked(api.getSnapshot).mockResolvedValue(fixtureSnapshots[LAB]);
    useConsoleStore.setState({
      route: { name: "server", serverId: "srv-unknown", tab: "overview" },
      selectedServerId: "srv-unknown",
      servers: [],
      snapshots: {},
      snapshotErrors: { "srv-unknown": "request failed" },
      statuses: {},
    });
    render(<ServerPage serverId="srv-unknown" />);

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Snapshot unavailable")).toBeTruthy();
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(api.getSnapshot).toHaveBeenCalledWith("srv-unknown"));
  });

  it("shows the offline error panel with identity for the offline fixture server", () => {
    seedServer("srv-gpu-node-3", "overview");
    render(<ServerPage serverId="srv-gpu-node-3" />);

    expect(screen.getByText("gpu-node-3")).toBeTruthy();
    expect(screen.getByText("Server unreachable")).toBeTruthy();
    expect(screen.getByText("ssh_closed")).toBeTruthy();
    expect(document.querySelector(".status-dot--offline")).toBeTruthy();
  });
});

describe("ServerPage — GPUs tab", () => {
  it("renders one expanded lane per GPU with that GPU's process rows", () => {
    seedServer(LAB, "gpus");
    render(<ServerPage serverId={LAB} />);

    expect(document.querySelectorAll(".gpu-lane-x")).toHaveLength(2);
    // only GPU0 holds a compute process; sparklines stay hidden with <2 points
    expect(document.querySelectorAll(".srv-gpu-proc:not(.srv-gpu-proc--head)")).toHaveLength(1);
    expect(screen.getByText("40211")).toBeTruthy();
    expect(document.querySelectorAll(".sparkline svg")).toHaveLength(0);
  });
});

describe("ServerPage — processes tab", () => {
  it("supports search, GPU-only filter, and header-click sorting", () => {
    seedServer(LAB, "processes");
    render(<ServerPage serverId={LAB} />);

    // default sort: CPU descending → the 712.4% python process leads
    expect(firstRowPid()).toBe("40211");
    expect(document.querySelectorAll(".srv-table tbody tr")).toHaveLength(5);

    // search matches name, user, and pid
    fireEvent.change(screen.getByLabelText("Search processes"), { target: { value: "dana" } });
    expect(document.querySelectorAll(".srv-table tbody tr")).toHaveLength(2);
    fireEvent.change(screen.getByLabelText("Search processes"), { target: { value: "1180" } });
    expect(document.querySelectorAll(".srv-table tbody tr")).toHaveLength(1);
    expect(firstRowPid()).toBe("1180");

    // no matches → explicit empty state
    fireEvent.change(screen.getByLabelText("Search processes"), { target: { value: "zzz" } });
    expect(screen.getByText("No matching processes")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Search processes"), { target: { value: "" } });

    // GPU-only toggle keeps only processes holding a GPU
    fireEvent.click(screen.getByText("GPU only"));
    expect(rowPids()).toEqual(["40211"]);
    fireEvent.click(screen.getByText("GPU only"));

    // sort by RSS: first click → descending (5.2 GB python), second → ascending (18 MB htop)
    fireEvent.click(screen.getByRole("button", { name: "RSS" }));
    expect(firstRowPid()).toBe("40211");
    fireEvent.click(screen.getByRole("button", { name: "RSS ↓" }));
    expect(firstRowPid()).toBe("3311");
  });

  it("caps rendering at 200 rows with a showing-N-of-M note", () => {
    const many: ProcessInfo[] = Array.from({ length: 210 }, (_unused, index) => ({
      pid: 10_000 + index,
      user: "demo",
      name: `p${index}`,
      cpu_percent: index,
      mem_percent: 1,
      rss_b: 1000,
      state: "S",
      command: `p${index}`,
      gpu_indexes: [],
      gpu_vram_b: null,
    }));
    seedSnapshot({ ...fixtureSnapshots[LAB], processes: many });
    useConsoleStore.setState({
      route: { name: "server", serverId: LAB, tab: "processes" },
      selectedTab: "processes",
    });
    render(<ServerPage serverId={LAB} />);

    expect(document.querySelectorAll(".srv-table tbody tr")).toHaveLength(200);
    expect(screen.getByText("showing 200 of 210")).toBeTruthy();
  });
});

describe("ServerPage — process actions", () => {
  function openConfirmDialog(action: "Terminate process" | "Kill process"): void {
    fireEvent.click(screen.getByLabelText("Actions for process 40211"));
    fireEvent.click(screen.getByRole("menuitem", { name: action }));
  }

  it("names action verb, server, PID and process name in the confirmation", () => {
    seedServer(LAB, "processes");
    render(<ServerPage serverId={LAB} />);
    openConfirmDialog("Terminate process");

    const dialog = screen.getByRole("dialog");
    // dialog title and confirm button both carry the verb + PID
    expect(screen.getAllByText("Terminate process 40211")).toHaveLength(2);
    // the lead names PID + process name + server display name
    expect(
      within(dialog).getByText("Terminate process 40211 (python) on lab-4090?"),
    ).toBeTruthy();
    expect(
      within(dialog).getByText("python train_cifar.py --batch-size 256 --epochs 120"),
    ).toBeTruthy();

    // kill variant
    fireEvent.click(screen.getByText("Cancel"));
    openConfirmDialog("Kill process");
    expect(screen.getAllByText("Kill process 40211")).toHaveLength(2);
    expect(screen.getByText("Kill process 40211 (python) on lab-4090?")).toBeTruthy();
  });

  it("calls terminateProcess, toasts success, and refreshes the snapshot", async () => {
    vi.mocked(api.getSnapshot).mockResolvedValue(fixtureSnapshots[LAB]);
    seedServer(LAB, "processes");
    render(<ServerPage serverId={LAB} />);
    openConfirmDialog("Terminate process");

    fireEvent.click(screen.getByRole("button", { name: "Terminate process 40211" }));
    await waitFor(() => expect(api.terminateProcess).toHaveBeenCalledWith(LAB, 40211));
    await waitFor(() => expect(api.getSnapshot).toHaveBeenCalledWith(LAB));
    expect(screen.getByText("Terminated 40211 — done")).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("maps API failure details to an inline error and keeps the dialog open", async () => {
    vi.mocked(api.terminateProcess).mockRejectedValue(new ApiError(409, "no such process"));
    seedServer(LAB, "processes");
    render(<ServerPage serverId={LAB} />);
    openConfirmDialog("Terminate process");

    fireEvent.click(screen.getByRole("button", { name: "Terminate process 40211" }));
    await waitFor(() =>
      expect(screen.getByText("no such process — it may have already exited")).toBeTruthy(),
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(api.getSnapshot).not.toHaveBeenCalled();

    // recognized details map; unknown ones pass through verbatim
    vi.mocked(api.killProcess).mockRejectedValue(new ApiError(403, "operation not permitted"));
    fireEvent.click(screen.getByText("Cancel"));
    openConfirmDialog("Kill process");
    fireEvent.click(screen.getByRole("button", { name: "Kill process 40211" }));
    await waitFor(() =>
      expect(screen.getByText("operation not permitted — you do not own this process")).toBeTruthy(),
    );
  });
});

describe("ServerPage — storage / network / system", () => {
  it("sorts mounts by usage descending with storage meter thresholds", () => {
    seedServer(LAB, "storage");
    render(<ServerPage serverId={LAB} />);

    const mounts = [...document.querySelectorAll(".srv-mount")];
    expect(mounts).toHaveLength(3);
    expect(mounts[0].textContent).toContain("/data"); // 82% — warn at 80
    expect(mounts[1].textContent).toContain("/"); // 60% — ok
    expect(mounts[2].textContent).toContain("/home"); // 31%
    expect(mounts[0].querySelector(".meter__seg--warn")).toBeTruthy();
    expect(mounts[1].querySelector(".meter__seg--ok")).toBeTruthy();
    expect(mounts[0].textContent).toContain("6.6/8.0 TB");
    expect(mounts[0].textContent).toContain("1.0 TB free");
  });

  it("renders network rates as an em dash when the host has no two samples yet", () => {
    seedSnapshot({
      ...fixtureSnapshots[LAB],
      network: [
        {
          name: "eth0",
          ip: "10.40.0.11",
          rx_bps: null,
          tx_bps: null,
          rx_total_b: 10745058181000,
          tx_total_b: 3_408_486_046_106, // 3.1 TiB
        },
      ],
    });
    useConsoleStore.setState({
      route: { name: "server", serverId: LAB, tab: "network" },
      selectedTab: "network",
    });
    render(<ServerPage serverId={LAB} />);

    expect(screen.getByText("eth0")).toBeTruthy();
    expect(screen.getByText("RX —")).toBeTruthy();
    expect(screen.getByText("TX —")).toBeTruthy();
    expect(screen.getByText("9.8 TB in · 3.1 TB out")).toBeTruthy();
  });

  it("renders system facts and switches sections when the tab changes", () => {
    seedServer(LAB, "system");
    const view = render(<ServerPage serverId={LAB} />);

    // hostname appears twice: as the section meta and as the hostname row
    expect(screen.getAllByText("lab-4090")).toHaveLength(2);
    expect(screen.getByText("Ubuntu 22.04.4 LTS")).toBeTruthy();
    expect(screen.getByText("5.15.0-107-generic")).toBeTruthy();
    expect(screen.getByText("12d 0h")).toBeTruthy(); // 1_036_800 s
    expect(screen.getByText("16 physical · 32 logical")).toBeTruthy();
    expect(screen.getByText("550.54.15")).toBeTruthy();

    // tab switching re-renders the matching section only
    useConsoleStore.setState({
      route: { name: "server", serverId: LAB, tab: "overview" },
      selectedTab: "overview",
    });
    view.rerender(<ServerPage serverId={LAB} />);
    expect(screen.getByText("Top processes")).toBeTruthy();
    expect(screen.queryByText("5.15.0-107-generic")).toBeNull();

    useConsoleStore.setState({
      route: { name: "server", serverId: LAB, tab: "storage" },
      selectedTab: "storage",
    });
    view.rerender(<ServerPage serverId={LAB} />);
    expect(screen.getByText("/data")).toBeTruthy();
    expect(screen.queryByText("Top processes")).toBeNull();
  });
});

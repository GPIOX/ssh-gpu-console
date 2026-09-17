import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FleetPage } from "../features/fleet/FleetPage";
import { statusSeverity, sortFleetEntries } from "../features/fleet/fleetSort";
import { api } from "../services/api";
import { useConsoleStore } from "../store/consoleStore";
import { fixtureFleetSummary, fixtureSnapshots } from "../data/fixtures";
import type { FleetEntry, FleetGpu, FleetSummary, ServerStatus } from "../types/models";

function jsonResponse(status: number, body: unknown): unknown {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 500 ? "Internal Server Error" : "OK",
    json: async () => body,
  };
}

function makeEntry(
  overrides: Partial<FleetEntry> & Pick<FleetEntry, "server_id" | "display_name">,
): FleetEntry {
  const base: FleetEntry = {
    server_id: overrides.server_id,
    display_name: overrides.display_name,
    ssh_endpoint: `u@${overrides.display_name}:22`,
    status: "online",
    enabled: true,
    os_pretty: null,
    gpu_model: null,
    gpu_count: 0,
    gpu_busy: 0,
    gpu_free: 0,
    cpu_percent: null,
    memory_percent: null,
    vram_used_b: null,
    vram_total_b: null,
    disk_warning: false,
    updated_at: null,
  };
  return { ...base, ...overrides };
}

function makeFleetGpu(
  overrides: Partial<FleetGpu> & Pick<FleetGpu, "index">,
): FleetGpu {
  return {
    utilization_percent: null,
    vram_used_b: null,
    vram_total_b: null,
    temperature_c: null,
    availability: "unavailable",
    ...overrides,
  };
}

function summaryOf(entries: FleetEntry[]): FleetSummary {
  return { generated_at: "2026-01-01T00:00:00Z", servers: entries };
}

function resetStore(): void {
  useConsoleStore.setState({
    fleetSummary: null,
    fleetLoading: false,
    fleetError: null,
    fleetSort: { key: "name", direction: "asc" },
    snapshots: {},
    statuses: {},
  });
}

function panelNames(): string[] {
  return [...document.querySelectorAll(".fleet-panel__name")].map(
    (node) => node.textContent ?? "",
  );
}

beforeEach(() => {
  window.location.hash = "#/fleet";
  resetStore();
});

afterEach(() => {
  resetStore();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.location.hash = "";
});

describe("fleet sort: severity ranking", () => {
  it("ranks the full status ladder worst-first", () => {
    const ladder: ServerStatus[] = [
      "offline",
      "timeout",
      "authentication_failed",
      "host_key_error",
      "reconnecting",
      "connecting",
      "unknown",
      "degraded",
      "online",
    ];
    const values = ladder.map((status) => statusSeverity(status));
    expect([...values].sort((a, b) => a - b)).toEqual(values);
    expect(new Set(values).size).toBe(ladder.length);
  });

  it("sorts by status severity ascending with a name tiebreak", () => {
    const entries = [
      makeEntry({ server_id: "a", display_name: "alpha", status: "online" }),
      makeEntry({ server_id: "b", display_name: "bravo", status: "offline" }),
      makeEntry({ server_id: "c", display_name: "charlie", status: "degraded" }),
      makeEntry({ server_id: "d", display_name: "delta", status: "connecting" }),
      makeEntry({ server_id: "e", display_name: "echo", status: "timeout" }),
    ];
    const sorted = sortFleetEntries(entries, { key: "status", direction: "asc" });
    expect(sorted.map((entry) => entry.display_name)).toEqual([
      "bravo", // offline
      "echo", // timeout
      "delta", // connecting
      "charlie", // degraded
      "alpha", // online
    ]);
  });

  it("sorts GPUs by count descending", () => {
    const entries = [
      makeEntry({ server_id: "z", display_name: "zeta", gpu_count: 2 }),
      makeEntry({ server_id: "y", display_name: "yankee", gpu_count: 8 }),
      makeEntry({ server_id: "x", display_name: "xray", gpu_count: 0 }),
    ];
    const sorted = sortFleetEntries(entries, { key: "gpus", direction: "desc" });
    expect(sorted.map((entry) => entry.display_name)).toEqual(["yankee", "zeta", "xray"]);
  });

  it("applies the store sort through the page (status asc, then desc on toggle)", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({ server_id: "a", display_name: "alpha", status: "online" }),
        makeEntry({ server_id: "b", display_name: "bravo", status: "offline" }),
      ]),
      fleetSort: { key: "status", direction: "asc" },
    });
    const { rerender } = render(<FleetPage />);
    expect(panelNames()).toEqual(["bravo", "alpha"]);

    useConsoleStore.setState({ fleetSort: { key: "status", direction: "desc" } });
    rerender(<FleetPage />);
    expect(panelNames()).toEqual(["alpha", "bravo"]);
  });
});

describe("fleet machine panel: FleetEntry fields", () => {
  it("renders identity, platform facts, freshness, meters, and the GPU summary line", () => {
    useConsoleStore.setState({ fleetSummary: fixtureFleetSummary, snapshots: {} });
    render(<FleetPage />);

    expect(screen.getByText("lab-4090")).toBeTruthy();
    expect(screen.getByText("demo@lab-4090:1111")).toBeTruthy();
    expect(screen.getByText("RTX 4090 ×2 · Ubuntu 22.04.4 LTS")).toBeTruthy();
    expect(screen.getAllByText(/^updated /).length).toBe(3);

    // pressure meters carry the entry's values
    expect(screen.getByLabelText("lab-4090 CPU")).toBeTruthy();
    expect(screen.getByLabelText("lab-4090 RAM")).toBeTruthy();

    // no snapshot → aggregate GPU summary derived from the entry + VRAM pair
    expect(screen.getByText("2 · 1 busy · 1 free")).toBeTruthy();
    expect(screen.getByText("18.8/48.0 GB")).toBeTruthy();
  });

  it("omits platform facts gracefully when both model and OS are absent", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({ server_id: "a", display_name: "alpha", gpu_model: null, os_pretty: null }),
      ]),
    });
    const { container } = render(<FleetPage />);
    expect(container.querySelector(".fleet-panel__platform")).toBeNull();
    expect(container.querySelector(".fleet-panel__gpus")).toBeTruthy();
  });
});

describe("fleet machine panel: GPU zone", () => {
  it("renders snapshot lanes with a +N more overflow line", () => {
    useConsoleStore.setState({ fleetSummary: fixtureFleetSummary, snapshots: fixtureSnapshots });
    const { container } = render(<FleetPage />);

    // dgx-a100: 8 snapshot GPUs → 4 visible lanes + "+4 more"; lab-4090: 2 lanes
    expect(screen.getByText("+4 more")).toBeTruthy();
    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(6);
  });

  it("falls back to the summary line when the snapshot has no GPUs", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "srv-lab-4090",
          display_name: "lab-4090",
          gpu_model: "RTX 4090",
          gpu_count: 2,
          gpu_busy: 1,
          gpu_free: 1,
        }),
      ]),
      snapshots: { "srv-lab-4090": { ...fixtureSnapshots["srv-lab-4090"], gpus: [] } },
    });
    const { container } = render(<FleetPage />);
    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(0);
    expect(screen.getByText("2 · 1 busy · 1 free")).toBeTruthy();
  });

  it("shows a quiet line for servers reporting zero GPUs", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([makeEntry({ server_id: "a", display_name: "alpha" })]),
    });
    const { container } = render(<FleetPage />);
    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(0);
    expect(screen.getByText("no GPUs reported")).toBeTruthy();
  });
});

describe("fleet machine panel: disk warning chip", () => {
  it("renders the widest mount percent only when disk_warning is set", () => {
    useConsoleStore.setState({ fleetSummary: fixtureFleetSummary, snapshots: fixtureSnapshots });
    render(<FleetPage />);

    expect(screen.getByText("disk 82%")).toBeTruthy(); // lab-4090 /data
    expect(screen.getByText("disk 91%")).toBeTruthy(); // dgx-a100 /scratch
    expect(screen.getAllByText(/^disk/)).toHaveLength(2); // gpu-node-3 has none
  });

  it("renders a bare disk chip when no snapshot carries mount data", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({ server_id: "a", display_name: "alpha", disk_warning: true }),
      ]),
    });
    render(<FleetPage />);
    expect(screen.getByText("disk")).toBeTruthy();
  });
});

describe("fleet machine panel: offline + stale states", () => {
  it("marks unreachable servers with the desaturation class", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({ server_id: "a", display_name: "alpha", status: "offline" }),
        makeEntry({ server_id: "b", display_name: "beta" }),
      ]),
    });
    const { container } = render(<FleetPage />);
    const panels = container.querySelectorAll(".fleet-panel");
    expect(panels).toHaveLength(2);
    expect(panels[0].className).toContain("fleet-panel--dim");
    expect(panels[1].className).not.toContain("fleet-panel--dim");
  });

  it("marks stale snapshots with the dim class and a stale chip", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "srv-lab-4090",
          display_name: "lab-4090",
          cpu_percent: 23.4,
          memory_percent: 40.9,
        }),
      ]),
      snapshots: {
        "srv-lab-4090": { ...fixtureSnapshots["srv-lab-4090"], stale: true },
      },
    });
    const { container } = render(<FleetPage />);
    expect(container.querySelector(".fleet-panel--stale")).toBeTruthy();
    expect(container.querySelector(".fleet-panel--dim")).toBeTruthy();
    expect(screen.getByText(/^stale · /)).toBeTruthy();
  });

  it("collapses disabled servers to a quiet identity line with a disabled chip", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({ server_id: "a", display_name: "alpha", enabled: false }),
      ]),
    });
    const { container } = render(<FleetPage />);
    expect(container.querySelector(".fleet-panel--disabled")).toBeTruthy();
    expect(screen.getByText("disabled")).toBeTruthy();
    expect(screen.queryByText("CPU")).toBeNull(); // no pressure band when collapsed

    fireEvent.click(screen.getByLabelText("Server actions: alpha"));
    expect(screen.getByText("Enable")).toBeTruthy();
  });
});

describe("fleet machine panel: fleet-tier GPU lanes (entry.gpus)", () => {
  it("renders compressed lanes with utilization, VRAM, and availability from entry.gpus", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "a",
          display_name: "alpha",
          gpu_count: 2,
          gpus: [
            makeFleetGpu({
              index: 0,
              utilization_percent: 72,
              vram_used_b: 19541268941,
              vram_total_b: 25769803776,
              temperature_c: 64,
              availability: "active",
            }),
            makeFleetGpu({ index: 1, utilization_percent: 3, availability: "free" }),
          ],
        }),
      ]),
    });
    const { container } = render(<FleetPage />);

    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(2);
    expect(screen.getByText("72%")).toBeTruthy(); // utilization meter value
    expect(screen.getByText("18.2/24.0 GB")).toBeTruthy(); // VRAM pair from FleetGpu bytes
    expect(screen.getByText("64°")).toBeTruthy(); // temperature
    expect(screen.getByText("ACTIVE")).toBeTruthy();
    expect(screen.getByText("FREE")).toBeTruthy();
  });

  it("prefers full snapshot lanes over entry.gpus", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "srv-lab-4090",
          display_name: "lab-4090",
          gpu_count: 2,
          gpus: [makeFleetGpu({ index: 0, utilization_percent: 11, availability: "free" })],
        }),
      ]),
      snapshots: fixtureSnapshots,
    });
    const { container } = render(<FleetPage />);

    // snapshot GPU0 runs at 72.4%; the 11% entry.gpus lane must not render
    expect(screen.getByText("72%")).toBeTruthy();
    expect(screen.queryByText("11%")).toBeNull();
    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(2);
  });

  it("shows '+N more' when entry.gpus exceeds four visible lanes", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "a",
          display_name: "alpha",
          gpu_count: 6,
          gpus: Array.from({ length: 6 }, (_unused, index) =>
            makeFleetGpu({ index, utilization_percent: index * 10, availability: "active" }),
          ),
        }),
      ]),
    });
    const { container } = render(<FleetPage />);

    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(4);
    expect(screen.getByText("+2 more")).toBeTruthy();
  });

  it("falls back to the aggregate summary when gpus is absent or empty", () => {
    useConsoleStore.setState({
      fleetSummary: summaryOf([
        makeEntry({
          server_id: "a",
          display_name: "alpha",
          gpu_count: 2,
          gpu_busy: 1,
          gpu_free: 1,
          gpus: [],
        }),
      ]),
    });
    const { container } = render(<FleetPage />);

    expect(container.querySelectorAll(".gpu-lane")).toHaveLength(0);
    expect(screen.getByText("2 · 1 busy · 1 free")).toBeTruthy();
  });
});

describe("fleet page: async states", () => {
  it("shows three shape-matched skeleton panels while loading", () => {
    useConsoleStore.setState({ fleetLoading: true });
    const { container } = render(<FleetPage />);
    expect(container.querySelectorAll(".fleet-skeleton")).toHaveLength(3);
  });

  it("shows an error panel with retry on fleet failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse(500, { detail: "backend down" })),
    );
    useConsoleStore.setState({ fleetError: "backend down" });
    render(<FleetPage />);

    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("backend down")).toBeTruthy();
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => {
      expect(useConsoleStore.getState().fleetError).toBe("backend down");
    });
    expect(screen.getByRole("alert")).toBeTruthy();
  });

  it("shows the empty state pointing at Settings for zero servers", () => {
    useConsoleStore.setState({ fleetSummary: summaryOf([]) });
    render(<FleetPage />);
    expect(screen.getByText("No servers yet")).toBeTruthy();
    expect(screen.getByText("Open Settings")).toBeTruthy();
  });
});

describe("fleet page: preview mode", () => {
  it("renders fixture panels and the full aggregate without store data", () => {
    const { container } = render(<FleetPage preview />);
    expect(container.querySelectorAll(".fleet-panel")).toHaveLength(3);
    expect(screen.getByText("lab-4090")).toBeTruthy();
    expect(screen.getByText("3 servers · 2 online · 10 GPUs · 1 free")).toBeTruthy();
    expect(screen.getByText("fixture data")).toBeTruthy();
  });

  it("activates on the #/preview hash without the prop", () => {
    window.location.hash = "#/preview";
    const { container } = render(<FleetPage />);
    expect(container.querySelectorAll(".fleet-panel")).toHaveLength(3);
    window.location.hash = "";
  });
});

describe("fleet machine panel: row menu", () => {
  function renderOneEntry(): void {
    useConsoleStore.setState({
      fleetSummary: summaryOf([makeEntry({ server_id: "srv-lab-4090", display_name: "lab-4090" })]),
    });
    render(<FleetPage />);
  }

  it("opens the overflow menu without triggering the row click", () => {
    renderOneEntry();
    expect(screen.queryByRole("menu")).toBeNull();

    fireEvent.click(screen.getByLabelText("Server actions: lab-4090"));
    expect(screen.getByRole("menu")).toBeTruthy();
    expect(screen.getByText("Test connection")).toBeTruthy();
    expect(screen.getByText("Open")).toBeTruthy();
    expect(screen.getByText("Disable")).toBeTruthy();
    expect(screen.getByText("Remove")).toBeTruthy();
    expect(window.location.hash).toBe("#/fleet");
  });

  it("clicking the panel navigates to the server overview", () => {
    renderOneEntry();
    fireEvent.click(screen.getByText("lab-4090"));
    expect(window.location.hash).toBe("#/server/srv-lab-4090/overview");
  });

  it("test connection shows the latency chip on success", async () => {
    const spy = vi.spyOn(api, "testConnection").mockResolvedValue({
      ok: true,
      status: "online",
      detail: "handshake ok",
      latency_ms: 34,
      pending_host_key: null,
    });
    renderOneEntry();
    fireEvent.click(screen.getByLabelText("Server actions: lab-4090"));
    fireEvent.click(screen.getByText("Test connection"));

    expect(await screen.findByText("online · 34 ms")).toBeTruthy();
    expect(spy).toHaveBeenCalledWith("srv-lab-4090");
  });

  it("test connection shows the classified failure status", async () => {
    vi.spyOn(api, "testConnection").mockResolvedValue({
      ok: false,
      status: "authentication_failed",
      detail: "permission denied (publickey)",
      latency_ms: 120,
      pending_host_key: null,
    });
    renderOneEntry();
    fireEvent.click(screen.getByLabelText("Server actions: lab-4090"));
    fireEvent.click(screen.getByText("Test connection"));

    expect(await screen.findByText("auth failed")).toBeTruthy();
  });

  it("remove opens a confirmation naming the server and calls deleteServer", async () => {
    const deleteSpy = vi.spyOn(api, "deleteServer").mockResolvedValue(undefined);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/telemetry/fleet")) {
          return jsonResponse(200, { generated_at: "2026-01-01T00:00:00Z", servers: [] });
        }
        return jsonResponse(200, []);
      }),
    );
    renderOneEntry();

    fireEvent.click(screen.getByLabelText("Server actions: lab-4090"));
    fireEvent.click(screen.getByText("Remove"));
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText(/Remove server/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Remove lab-4090" }));
    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith("srv-lab-4090");
    });
    // registry + fleet refresh: the removed server leaves the page
    expect(await screen.findByText("No servers yet")).toBeTruthy();
  });
});

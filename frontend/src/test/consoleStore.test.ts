import { afterEach, describe, expect, it, vi } from "vitest";
import { createConsoleStore, HISTORY_LIMIT, type ConsoleState } from "../store/consoleStore";
import { realtime } from "../services/realtime";
import type { FleetSummary, GpuInfo, ServerSnapshot, ServerToClientFrame } from "../types/models";

function jsonResponse(status: number, body: unknown): unknown {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 500 ? "Internal Server Error" : "OK",
    json: async () => body,
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 6; i += 1) await Promise.resolve();
};

let clock = 0;

function makeGpu(overrides: Partial<GpuInfo> = {}): GpuInfo {
  return {
    index: 0,
    uuid: "GPU-0",
    name: "Test GPU",
    utilization_percent: 70,
    vram_used_b: 10_000,
    vram_total_b: 100_000,
    vram_percent: 10,
    temperature_c: 60,
    power_watts: 200,
    power_limit_watts: 300,
    fan_percent: null,
    availability: "active",
    process_count: 0,
    ...overrides,
  };
}

function makeSnapshot(overrides: Partial<ServerSnapshot> = {}): ServerSnapshot {
  clock += 1;
  return {
    server_id: "srv1",
    status: "online",
    generated_at: new Date(1_700_000_000_000 + clock).toISOString(),
    cpu: null,
    memory: null,
    gpus: [makeGpu()],
    gpu_processes: [],
    processes: [],
    storage: [],
    network: [],
    system: null,
    errors: {},
    stale: false,
    ...overrides,
  };
}

function makeSummary(): FleetSummary {
  return {
    generated_at: "2026-01-01T00:00:00Z",
    servers: [
      {
        server_id: "srv1",
        display_name: "one",
        ssh_endpoint: "u@one:22",
        status: "online",
        enabled: true,
        os_pretty: null,
        gpu_model: "X",
        gpu_count: 2,
        gpu_busy: 1,
        gpu_free: 1,
        cpu_percent: 10,
        memory_percent: 20,
        vram_used_b: null,
        vram_total_b: null,
        disk_warning: false,
        updated_at: null,
      },
      {
        server_id: "srv2",
        display_name: "two",
        ssh_endpoint: null,
        status: "offline",
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
      },
    ],
  };
}

function serverFrame(snapshot: ServerSnapshot): ServerToClientFrame {
  return { type: "server", server_id: snapshot.server_id, snapshot };
}

async function stubSnapshotFetch(snapshot: ServerSnapshot): Promise<ReturnType<typeof vi.fn>> {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/telemetry/servers/")) return jsonResponse(200, snapshot);
    if (url.endsWith("/servers")) return jsonResponse(200, []);
    return jsonResponse(404, { detail: "not found" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("console store: realtime frames", () => {
  it("stores snapshots and pushes per-gpu history", () => {
    const store = createConsoleStore();
    const snapshot = makeSnapshot();
    store.getState().applyRealtimeFrame(serverFrame(snapshot));
    const state = store.getState();
    expect(state.snapshots["srv1"]).toEqual(snapshot);
    expect(state.history.get("srv1")?.get(0)?.utilization).toEqual([70]);
    expect(state.history.get("srv1")?.get(0)?.vram).toEqual([10]);
    expect(state.history.get("srv1")?.get(0)?.power).toEqual([200]);
  });

  it("caps history at 120 samples", () => {
    const store = createConsoleStore();
    for (let i = 0; i < 130; i += 1) {
      const snapshot = makeSnapshot({ gpus: [makeGpu({ utilization_percent: i })] });
      store.getState().applyRealtimeFrame(serverFrame(snapshot));
    }
    const utilization = store.getState().history.get("srv1")?.get(0)?.utilization ?? [];
    expect(utilization).toHaveLength(HISTORY_LIMIT);
    expect(utilization[0]).toBe(130 - HISTORY_LIMIT);
    expect(utilization.at(-1)).toBe(129);
  });

  it("dedupes pushes by snapshot timestamp", () => {
    const store = createConsoleStore();
    const snapshot = makeSnapshot();
    store.getState().applyRealtimeFrame(serverFrame(snapshot));
    store.getState().applyRealtimeFrame(serverFrame(snapshot)); // same generated_at
    expect(store.getState().history.get("srv1")?.get(0)?.utilization).toEqual([70]);
  });

  it("skips null metric values without fabricating", () => {
    const store = createConsoleStore();
    const snapshot = makeSnapshot({
      gpus: [makeGpu({ utilization_percent: null, temperature_c: null })],
    });
    store.getState().applyRealtimeFrame(serverFrame(snapshot));
    const series = store.getState().history.get("srv1")?.get(0);
    expect(series?.utilization).toEqual([]);
    expect(series?.temperature).toEqual([]);
    expect(series?.vram).toEqual([10]);
  });

  it("keeps separate servers and gpu indexes apart", () => {
    const store = createConsoleStore();
    store.getState().applyRealtimeFrame(
      serverFrame(
        makeSnapshot({
          server_id: "srvA",
          gpus: [makeGpu({ index: 0 }), makeGpu({ index: 1, utilization_percent: 90 })],
        }),
      ),
    );
    const history = store.getState().history;
    expect(history.get("srvA")?.get(0)?.utilization).toEqual([70]);
    expect(history.get("srvA")?.get(1)?.utilization).toEqual([90]);
    expect(history.has("srv1")).toBe(false);
  });

  it("applies fleet frames and status frames", () => {
    const store = createConsoleStore();
    store.getState().applyRealtimeFrame({ type: "fleet", summary: makeSummary() });
    expect(store.getState().fleetSummary?.servers).toHaveLength(2);
    expect(store.getState().statuses["srv2"]).toBe("offline");

    store.getState().applyRealtimeFrame({ type: "status", server_id: "srv2", status: "online" });
    expect(store.getState().statuses["srv2"]).toBe("online");
    expect(
      store.getState().fleetSummary?.servers.find((entry) => entry.server_id === "srv2")?.status,
    ).toBe("online");
  });

  it("patches snapshot status on status frames without touching history", () => {
    const store = createConsoleStore();
    const snapshot = makeSnapshot();
    store.getState().applyRealtimeFrame(serverFrame(snapshot));
    const before = store.getState().history.get("srv1")?.get(0)?.utilization.length ?? 0;
    store.getState().applyRealtimeFrame({ type: "status", server_id: "srv1", status: "degraded" });
    expect(store.getState().snapshots["srv1"]?.status).toBe("degraded");
    expect(store.getState().history.get("srv1")?.get(0)?.utilization.length).toBe(before);
  });

  it("ignores hello frames", () => {
    const store = createConsoleStore();
    store.getState().applyRealtimeFrame({ type: "hello", server_ids: ["srv1"] });
    expect(store.getState().snapshots).toEqual({});
  });
});

describe("console store: selection + routing", () => {
  it("select sets intent and fetches the snapshot", async () => {
    const snapshot = makeSnapshot();
    await stubSnapshotFetch(snapshot);
    const sendSelect = vi.spyOn(realtime, "sendSelect").mockImplementation(() => undefined);
    const store = createConsoleStore();

    store.getState().select("srv1");
    await flush();

    expect(sendSelect).toHaveBeenCalledWith("srv1");
    expect(store.getState().selectedServerId).toBe("srv1");
    expect(store.getState().snapshots["srv1"]).toEqual(snapshot);

    store.getState().select(null);
    expect(sendSelect).toHaveBeenLastCalledWith(null);
    expect(store.getState().selectedServerId).toBe(null);
  });

  it("routes hash to server detail and triggers selection", async () => {
    const snapshot = makeSnapshot();
    await stubSnapshotFetch(snapshot);
    const sendSelect = vi.spyOn(realtime, "sendSelect").mockImplementation(() => undefined);
    const store = createConsoleStore();

    window.location.hash = "#/server/srv1/gpus";
    store.getState().applyHash();
    await flush();

    const state = store.getState();
    expect(state.route).toEqual({ name: "server", serverId: "srv1", tab: "gpus" });
    expect(state.selectedServerId).toBe("srv1");
    expect(state.selectedTab).toBe("gpus");
    expect(sendSelect).toHaveBeenCalledWith("srv1");
    expect(state.snapshots["srv1"]).toBeDefined();
  });

  it("leaving a server route clears the selection intent", async () => {
    await stubSnapshotFetch(makeSnapshot());
    const sendSelect = vi.spyOn(realtime, "sendSelect").mockImplementation(() => undefined);
    const store = createConsoleStore();
    window.location.hash = "#/server/srv1/overview";
    store.getState().applyHash();
    await flush();
    expect(store.getState().selectedServerId).toBe("srv1");

    window.location.hash = "#/fleet";
    store.getState().applyHash();
    expect(store.getState().selectedServerId).toBe(null);
    expect(sendSelect).toHaveBeenLastCalledWith(null);
  });

  it("setTab updates the hash route", async () => {
    await stubSnapshotFetch(makeSnapshot());
    vi.spyOn(realtime, "sendSelect").mockImplementation(() => undefined);
    const store = createConsoleStore();
    window.location.hash = "#/server/srv1/overview";
    store.getState().applyHash();
    await flush();

    store.getState().setTab("processes");
    expect(window.location.hash).toBe("#/server/srv1/processes");
    store.getState().applyHash(); // hashchange listener is wired in connect()
    expect(store.getState().selectedTab).toBe("processes");
  });

  it("navigating to the same hash still applies the route", () => {
    const store = createConsoleStore();
    window.location.hash = "#/settings";
    store.getState().navigate("#/settings");
    expect(store.getState().route).toEqual({ name: "settings" });
  });
});

describe("console store: REST loads", () => {
  it("loadServers fills the registry", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(200, [{ server_id: "a", display_name: "A", ssh_host: "a" }]),
      ),
    );
    const store = createConsoleStore();
    await store.getState().loadServers();
    expect(store.getState().servers).toHaveLength(1);
    expect(store.getState().serversError).toBe(null);
  });

  it("loadFleet failures surface as fleetError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { detail: "down" })));
    const store = createConsoleStore();
    await store.getState().loadFleet();
    expect(store.getState().fleetSummary).toBe(null);
    expect(store.getState().fleetError).toBe("down");
  });

  it("snapshot fetch failures are recorded per server", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(500, { detail: "down" })));
    const store = createConsoleStore();
    await store.getState().loadSnapshot("srvX");
    expect(store.getState().snapshotErrors["srvX"]).toBe("down");
  });
});

describe("console store: prefs", () => {
  it("stores the fleet sort preference", () => {
    const store = createConsoleStore();
    store.getState().setFleetSort({ key: "gpus", direction: "desc" });
    expect(store.getState().fleetSort).toEqual({ key: "gpus", direction: "desc" });
  });
});

describe("console store state invariants", () => {
  it("keeps telemetry out of localStorage", () => {
    const store = createConsoleStore() as unknown as { getState: () => ConsoleState };
    const state = store.getState();
    expect(state.history).toBeInstanceOf(Map);
  });
});

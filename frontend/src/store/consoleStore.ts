/**
 * The single zustand store: registry, routing, selection intent, per-server
 * snapshots, fleet summary, connection state, and bounded per-GPU history.
 * Telemetry lives only in bounded RAM (≤120 samples per series) — never in
 * localStorage.
 */

import { create } from "zustand";
import { api } from "../services/api";
import { realtime, type RealtimeState } from "../services/realtime";
import type {
  FleetSummary,
  ServerRecord,
  ServerSnapshot,
  ServerStatus,
  ServerToClientFrame,
} from "../types/models";
import { DEFAULT_TAB, parseHash, type Route, type ServerTab } from "../shell/routes";

export const HISTORY_LIMIT = 120;

export interface GpuHistorySeries {
  utilization: number[];
  vram: number[];
  temperature: number[];
  power: number[];
}

export interface FleetSort {
  key: "name" | "status" | "gpus";
  direction: "asc" | "desc";
}

export type ConnectionState = RealtimeState;

export interface ConsoleState {
  // connection
  connected: boolean;
  connection: ConnectionState;
  // registry
  servers: ServerRecord[];
  serversLoading: boolean;
  serversError: string | null;
  // routing + selection
  route: Route;
  selectedServerId: string | null;
  selectedTab: ServerTab;
  // telemetry
  fleetSummary: FleetSummary | null;
  fleetLoading: boolean;
  fleetError: string | null;
  /** Statuses seen via frames/REST for servers that may lack a fleet entry yet. */
  statuses: Record<string, ServerStatus>;
  snapshots: Record<string, ServerSnapshot>;
  snapshotErrors: Record<string, string>;
  /** Bounded per-server, per-GPU history. Map<serverId, Map<gpuIndex, series>>. */
  history: Map<string, Map<number, GpuHistorySeries>>;
  /** Last pushed snapshot timestamp per server, for timestamp dedupe. */
  historyTimestamps: Record<string, string>;
  fleetSort: FleetSort;
  // actions
  connect: () => void;
  applyHash: () => void;
  navigate: (hash: string) => void;
  select: (serverId: string | null) => void;
  setTab: (tab: ServerTab) => void;
  loadServers: () => Promise<void>;
  loadFleet: () => Promise<void>;
  loadSnapshot: (serverId: string) => Promise<void>;
  applyRealtimeFrame: (frame: ServerToClientFrame) => void;
  ingestSnapshot: (snapshot: ServerSnapshot) => void;
  setFleetSort: (sort: FleetSort) => void;
}

function pushSeries(previous: number[], value: number | null): number[] {
  if (value === null || !Number.isFinite(value)) return previous; // never fabricate
  const next =
    previous.length >= HISTORY_LIMIT
      ? previous.slice(previous.length - HISTORY_LIMIT + 1)
      : previous.slice();
  next.push(value);
  return next;
}

function pushHistoryInto(
  history: Map<string, Map<number, GpuHistorySeries>>,
  snapshot: ServerSnapshot,
): Map<string, Map<number, GpuHistorySeries>> {
  const serverHistory = new Map(history.get(snapshot.server_id) ?? []);
  for (const gpu of snapshot.gpus) {
    const previous =
      serverHistory.get(gpu.index) ?? {
        utilization: [],
        vram: [],
        temperature: [],
        power: [],
      };
    serverHistory.set(gpu.index, {
      utilization: pushSeries(previous.utilization, gpu.utilization_percent),
      vram: pushSeries(previous.vram, gpu.vram_percent),
      temperature: pushSeries(previous.temperature, gpu.temperature_c),
      power: pushSeries(previous.power, gpu.power_watts),
    });
  }
  const next = new Map(history);
  next.set(snapshot.server_id, serverHistory);
  return next;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "request failed";
}

export const createConsoleStore = () =>
  create<ConsoleState>()((set, get) => ({
    connected: false,
    connection: "connecting",
    servers: [],
    serversLoading: false,
    serversError: null,
    route: { name: "fleet" },
    selectedServerId: null,
    selectedTab: DEFAULT_TAB,
    fleetSummary: null,
    fleetLoading: false,
    fleetError: null,
    statuses: {},
    snapshots: {},
    snapshotErrors: {},
    history: new Map(),
    historyTimestamps: {},
    fleetSort: { key: "name", direction: "asc" },

    connect: () => {
      if (get().connected) return; // StrictMode-safe
      set({ connected: true });
      realtime.onFrame((frame) => get().applyRealtimeFrame(frame));
      realtime.onState((connection) => set({ connection }));
      realtime.start();
      realtime.sendSelect(get().selectedServerId);
      window.addEventListener("hashchange", () => get().applyHash());
      get().applyHash();
      void get().loadServers();
      void get().loadFleet();
    },

    applyHash: () => {
      const route = parseHash(window.location.hash);
      const previous = get().route;
      set({ route });
      if (route.name === "server") {
        set({ selectedTab: route.tab });
        if (previous.name !== "server" || previous.serverId !== route.serverId) {
          get().select(route.serverId);
        }
      } else if (get().selectedServerId !== null) {
        get().select(null);
      }
    },

    navigate: (hash) => {
      if (window.location.hash === hash) {
        get().applyHash(); // same-hash navigation fires no hashchange event
        return;
      }
      window.location.hash = hash;
    },

    select: (serverId) => {
      set({ selectedServerId: serverId });
      realtime.sendSelect(serverId);
      if (serverId !== null) void get().loadSnapshot(serverId);
    },

    setTab: (tab) => {
      const { route } = get();
      if (route.name !== "server") return;
      get().navigate(`#/server/${encodeURIComponent(route.serverId)}/${tab}`);
    },

    loadServers: async () => {
      set({ serversLoading: true, serversError: null });
      try {
        const servers = await api.listServers();
        set({ servers, serversLoading: false });
      } catch (cause) {
        set({ serversError: errorMessage(cause), serversLoading: false });
      }
    },

    loadFleet: async () => {
      set({ fleetLoading: true, fleetError: null });
      try {
        const fleetSummary = await api.getFleet();
        const statuses = { ...get().statuses };
        for (const entry of fleetSummary.servers) statuses[entry.server_id] = entry.status;
        set({ fleetSummary, fleetLoading: false, fleetError: null, statuses });
      } catch (cause) {
        set({ fleetError: errorMessage(cause), fleetLoading: false });
      }
    },

    loadSnapshot: async (serverId) => {
      try {
        const snapshot = await api.getSnapshot(serverId);
        get().ingestSnapshot(snapshot);
      } catch (cause) {
        set({ snapshotErrors: { ...get().snapshotErrors, [serverId]: errorMessage(cause) } });
      }
    },

    ingestSnapshot: (snapshot) => {
      const state = get();
      const lastAt = state.historyTimestamps[snapshot.server_id];
      const duplicate =
        snapshot.generated_at !== null && lastAt !== undefined && lastAt === snapshot.generated_at;
      const snapshots = { ...state.snapshots, [snapshot.server_id]: snapshot };
      const statuses = { ...state.statuses, [snapshot.server_id]: snapshot.status };
      const snapshotErrors = { ...state.snapshotErrors };
      delete snapshotErrors[snapshot.server_id];
      if (duplicate) {
        // Same generated_at as the last pushed sample — refresh state only.
        set({ snapshots, statuses, snapshotErrors });
        return;
      }
      set({
        snapshots,
        statuses,
        snapshotErrors,
        history: pushHistoryInto(state.history, snapshot),
        historyTimestamps:
          snapshot.generated_at !== null
            ? { ...state.historyTimestamps, [snapshot.server_id]: snapshot.generated_at }
            : state.historyTimestamps,
      });
    },

    applyRealtimeFrame: (frame) => {
      switch (frame.type) {
        case "hello":
          return;
        case "fleet": {
          const statuses = { ...get().statuses };
          for (const entry of frame.summary.servers) statuses[entry.server_id] = entry.status;
          set({
            fleetSummary: frame.summary,
            fleetLoading: false,
            fleetError: null,
            statuses,
          });
          return;
        }
        case "server":
          get().ingestSnapshot(frame.snapshot);
          return;
        case "status": {
          const { server_id, status } = frame;
          const state = get();
          const patch: Partial<ConsoleState> = {
            statuses: { ...state.statuses, [server_id]: status },
          };
          const snapshot = state.snapshots[server_id];
          if (snapshot !== undefined) {
            patch.snapshots = {
              ...state.snapshots,
              [server_id]: { ...snapshot, status },
            };
          }
          const fleet = state.fleetSummary;
          if (fleet !== null) {
            patch.fleetSummary = {
              ...fleet,
              servers: fleet.servers.map((entry) =>
                entry.server_id === server_id ? { ...entry, status } : entry,
              ),
            };
          }
          set(patch);
          return;
        }
      }
    },

    setFleetSort: (fleetSort) => set({ fleetSort }),
  }));

export type ConsoleStoreApi = ReturnType<typeof createConsoleStore>;

/** App-wide singleton. Tests use createConsoleStore() for a fresh instance. */
export const useConsoleStore = createConsoleStore();

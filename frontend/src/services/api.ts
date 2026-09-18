/**
 * Typed REST client for the /api/v1 control plane. All responses pass through
 * defensive normalizers; failures raise ApiError carrying status + detail.
 */

import type {
  AliasEntry,
  ConnectionTestResult,
  DirectAuthList,
  DirectAuthPeer,
  FleetSummary,
  HistoryPoint,
  HostKeyPrompt,
  PasswordCredentialStatus,
  ServerAuthStatus,
  ServerCreate,
  ServerPatch,
  ServerRecord,
  ServerSnapshot,
} from "../types/models";
import {
  normalizeAliasEntry,
  normalizeConnectionTestResult,
  normalizeDirectAuthList,
  normalizeDirectAuthPeer,
  normalizeFleetSummary,
  normalizeHistoryPoints,
  normalizeServerAuthStatus,
  normalizeServerRecord,
  normalizeServerSnapshot,
} from "./normalize";

/** REST base for the control plane (surfaced in Settings → Preferences). */
export const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

async function request(path: string, method: Method, body?: unknown): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError(0, cause instanceof Error ? cause.message : "network error");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`.trim();
    try {
      const payload: unknown = await response.json();
      if (typeof payload === "object" && payload !== null && "detail" in payload) {
        const rawDetail = (payload as { detail: unknown }).detail;
        if (typeof rawDetail === "string") detail = rawDetail;
      }
    } catch {
      // non-JSON error body — keep the status-line detail
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined;
  try {
    return await response.json();
  } catch {
    throw new ApiError(response.status, "malformed response body");
  }
}

function invalidPayload(what: string): ApiError {
  return new ApiError(502, `invalid ${what} payload`);
}

/** Metric name for the per-GPU history endpoint. `vram` maps to the backend's VRAM series. */
export type GpuHistoryMetric = "utilization" | "vram" | "temperature" | "power";

/** Subset of GET /health the Settings page surfaces (defensively extracted). */
export interface HealthInfo {
  status: string;
  scheduler_mode: string | null;
  realtime_clients: number | null;
}

export const api = {
  async listServers(): Promise<ServerRecord[]> {
    const raw = await request("/servers", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("servers");
    return raw
      .map(normalizeServerRecord)
      .filter((record): record is ServerRecord => record !== null);
  },

  async createServer(body: ServerCreate): Promise<ServerRecord> {
    const raw = await request("/servers", "POST", body);
    const record = normalizeServerRecord(raw);
    if (record === null) throw invalidPayload("server");
    return record;
  },

  async patchServer(serverId: string, patch: ServerPatch): Promise<ServerRecord> {
    const raw = await request(`/servers/${encodeURIComponent(serverId)}`, "PATCH", patch);
    const record = normalizeServerRecord(raw);
    if (record === null) throw invalidPayload("server");
    return record;
  },

  async deleteServer(serverId: string): Promise<void> {
    await request(`/servers/${encodeURIComponent(serverId)}`, "DELETE");
  },

  async listSshAliases(): Promise<AliasEntry[]> {
    const raw = await request("/servers/ssh-config/aliases", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("aliases");
    return raw
      .map(normalizeAliasEntry)
      .filter((entry): entry is AliasEntry => entry !== null);
  },

  async testConnection(serverId: string): Promise<ConnectionTestResult> {
    const raw = await request(`/servers/${encodeURIComponent(serverId)}/test`, "POST", {});
    const result = normalizeConnectionTestResult(raw);
    if (result === null) throw invalidPayload("connection test result");
    return result;
  },

  /** Explicit TOFU consent. Body shape is not pinned by the contract; the
   *  HostKeyPrompt is echoed so the backend can persist what was approved. */
  async trustHostKey(serverId: string, prompt: HostKeyPrompt): Promise<unknown> {
    return request(`/servers/${encodeURIComponent(serverId)}/host-key/trust`, "POST", prompt);
  },

  /** SSH-config resolution facts for one server (zero SSH; never a password). */
  async getServerAuth(serverId: string): Promise<ServerAuthStatus> {
    const raw = await request(`/servers/${encodeURIComponent(serverId)}/auth`, "GET");
    const status = normalizeServerAuthStatus(raw);
    if (status === null) throw invalidPayload("server auth status");
    return status;
  },

  /** Store a password credential server-side (system keyring or session-only). */
  async setPassword(serverId: string, password: string): Promise<PasswordCredentialStatus> {
    const raw = await request(
      `/servers/${encodeURIComponent(serverId)}/credentials/password`,
      "PUT",
      { password },
    );
    const storage =
      typeof raw === "object" && raw !== null && !Array.isArray(raw)
        ? (raw as { storage?: unknown }).storage
        : undefined;
    if (
      typeof raw !== "object" ||
      raw === null ||
      Array.isArray(raw) ||
      (raw as { configured?: unknown }).configured !== true ||
      (storage !== "system_keyring" && storage !== "session_only")
    ) {
      throw invalidPayload("password credential status");
    }
    return raw as PasswordCredentialStatus;
  },

  /** Remove a stored password credential. */
  async deletePassword(serverId: string): Promise<void> {
    await request(`/servers/${encodeURIComponent(serverId)}/credentials/password`, "DELETE");
  },

  /** Zero-SSH metadata: every direct-auth pair (BOTH directions) involving
   *  this server — sources that can reach it and targets it can reach. */
  async getDirectAuth(serverId: string): Promise<DirectAuthList> {
    const raw = await request(`/servers/${encodeURIComponent(serverId)}/direct-auth`, "GET");
    const list = normalizeDirectAuthList(raw);
    if (list === null) throw invalidPayload("direct-auth list");
    return list;
  },

  /** Explicit SSH preflight for one (source → target) pair. */
  async checkDirectAuth(sourceId: string, targetId: string): Promise<DirectAuthPeer> {
    const raw = await request(
      `/servers/${encodeURIComponent(sourceId)}/direct-auth/${encodeURIComponent(targetId)}/check`,
      "POST",
      {},
    );
    const peer = normalizeDirectAuthPeer(raw);
    if (peer === null) throw invalidPayload("direct-auth peer");
    return peer;
  },

  /** Install the SGC dedicated transfer key on the pair (explicit action). */
  async setupDirectKey(sourceId: string, targetId: string): Promise<DirectAuthPeer> {
    const raw = await request(
      `/servers/${encodeURIComponent(sourceId)}/direct-auth/${encodeURIComponent(targetId)}/setup-key`,
      "POST",
      {},
    );
    const peer = normalizeDirectAuthPeer(raw);
    if (peer === null) throw invalidPayload("direct-auth peer");
    return peer;
  },

  /** Remove the dedicated transfer key for the pair. */
  async revokeDirectAuth(sourceId: string, targetId: string): Promise<void> {
    await request(
      `/servers/${encodeURIComponent(sourceId)}/direct-auth/${encodeURIComponent(targetId)}`,
      "DELETE",
    );
  },

  async getHealth(): Promise<HealthInfo> {
    const raw = await request("/health", "GET");
    if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
      throw invalidPayload("health");
    }
    const body = raw as Record<string, unknown>;
    return {
      status: typeof body.status === "string" ? body.status : "unknown",
      scheduler_mode: typeof body.scheduler_mode === "string" ? body.scheduler_mode : null,
      realtime_clients:
        typeof body.realtime_clients === "number" && Number.isFinite(body.realtime_clients)
          ? body.realtime_clients
          : null,
    };
  },

  async getFleet(): Promise<FleetSummary> {
    const raw = await request("/telemetry/fleet", "GET");
    const summary = normalizeFleetSummary(raw);
    if (summary === null) throw invalidPayload("fleet summary");
    return summary;
  },

  async getSnapshot(serverId: string): Promise<ServerSnapshot> {
    const raw = await request(`/telemetry/servers/${encodeURIComponent(serverId)}`, "GET");
    const snapshot = normalizeServerSnapshot(raw);
    if (snapshot === null) throw invalidPayload("snapshot");
    return snapshot;
  },

  async getGpuHistory(
    serverId: string,
    gpu: number,
    metric: GpuHistoryMetric,
  ): Promise<HistoryPoint[]> {
    const query = new URLSearchParams({ gpu: String(gpu), metric });
    const raw = await request(
      `/telemetry/servers/${encodeURIComponent(serverId)}/gpu-history?${query.toString()}`,
      "GET",
    );
    return normalizeHistoryPoints(raw);
  },

  async terminateProcess(serverId: string, pid: number): Promise<void> {
    await request(`/servers/${encodeURIComponent(serverId)}/actions/terminate-process`, "POST", {
      pid,
    });
  },

  async killProcess(serverId: string, pid: number): Promise<void> {
    await request(`/servers/${encodeURIComponent(serverId)}/actions/kill-process`, "POST", { pid });
  },
};

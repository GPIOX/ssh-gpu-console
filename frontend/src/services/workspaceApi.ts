/**
 * Typed REST client for the workspace control plane (/api/v1/workspace).
 * Mirrors services/api.ts patterns: one request helper, defensive
 * normalization, ApiError with status + human detail. AppError bodies from the
 * backend carry {code, message} (not {detail}), and pydantic 422 bodies carry
 * {detail: [...]}, so the error extractor accepts both shapes.
 */

import { API_BASE, ApiError } from "./api";
import { isRecord } from "./normalize";
import { normalizeTransferBatch } from "./transfersApi";
import type {
  ArtifactCreate,
  ArtifactKind,
  ArtifactPatch,
  ArtifactRecord,
  ArtifactSyncItem,
  DistributionItem,
  DistributionState,
  InspectionState,
  LaunchConfigCreate,
  LaunchConfigPatch,
  LaunchConfigRecord,
  PlacementCreate,
  PlacementInspection,
  PlacementPatch,
  PlacementRecord,
  ProjectCreate,
  ProjectDistribution,
  ProjectPatch,
  ProjectRecord,
  ProjectSyncPlan,
  ServerRoots,
  ServerRootsUpdate,
  SyncAction,
  SyncPlanRequest,
} from "../types/workspace";
import type { TransferBatch, TransferStrategy } from "../types/transfers";

const ARTIFACT_KINDS = ["code", "dataset", "model"] as const;

export function artifactKind(value: unknown): ArtifactKind {
  return typeof value === "string" && (ARTIFACT_KINDS as readonly string[]).includes(value)
    ? (value as ArtifactKind)
    : "code";
}

const INSPECTION_STATES = ["declared", "verified", "missing", "unavailable"] as const;

function inspectionState(value: unknown): InspectionState {
  return typeof value === "string" && (INSPECTION_STATES as readonly string[]).includes(value)
    ? (value as InspectionState)
    : "declared";
}

const DISTRIBUTION_STATES = ["declared", "verified", "missing", "unavailable", "syncing"] as const;

function distributionState(value: unknown): DistributionState {
  return typeof value === "string" && (DISTRIBUTION_STATES as readonly string[]).includes(value)
    ? (value as DistributionState)
    : "declared";
}

const SYNC_ACTIONS = ["skip", "transfer", "unresolved"] as const;

function syncAction(value: unknown): SyncAction {
  // Unknown/absent action degrades to unresolved: the dialog then refuses the
  // sync instead of guessing a transfer.
  return typeof value === "string" && (SYNC_ACTIONS as readonly string[]).includes(value)
    ? (value as SyncAction)
    : "unresolved";
}

const STRATEGY_VALUES = ["auto", "direct_rsync", "local_relay"] as const;

function strategyOrNull(value: unknown): TransferStrategy | null {
  return typeof value === "string" && (STRATEGY_VALUES as readonly string[]).includes(value)
    ? (value as TransferStrategy)
    : null;
}

// -- primitive guards (local: normalize.ts exports only isRecord) ------------

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function intOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function strList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function strRecord(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string") out[key] = item;
  }
  return out;
}

function optionalStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

// -- normalizers --------------------------------------------------------------

export function normalizeProjectRecord(raw: unknown): ProjectRecord | null {
  if (!isRecord(raw)) return null;
  const projectId = str(raw.project_id);
  const name = str(raw.name);
  if (projectId === null || name === null) return null;
  return {
    project_id: projectId,
    name,
    description: typeof raw.description === "string" ? raw.description : "",
    artifact_ids: strList(raw.artifact_ids),
    launch_config_ids: strList(raw.launch_config_ids),
    tags: strList(raw.tags),
    transfer_excludes: strList(raw.transfer_excludes),
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : "",
  };
}

export function normalizeArtifactRecord(raw: unknown): ArtifactRecord | null {
  if (!isRecord(raw)) return null;
  const artifactId = str(raw.artifact_id);
  const name = str(raw.name);
  if (artifactId === null || name === null) return null;
  return {
    artifact_id: artifactId,
    kind: artifactKind(raw.kind),
    name,
    version: optionalStr(raw.version),
    description: typeof raw.description === "string" ? raw.description : "",
    immutable: bool(raw.immutable, true),
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : "",
  };
}

export function normalizePlacementRecord(raw: unknown): PlacementRecord | null {
  if (!isRecord(raw)) return null;
  const placementId = str(raw.placement_id);
  const artifactId = str(raw.artifact_id);
  const serverId = str(raw.server_id);
  const remotePath = str(raw.remote_path);
  if (placementId === null || artifactId === null || serverId === null || remotePath === null) {
    return null;
  }
  return {
    placement_id: placementId,
    artifact_id: artifactId,
    server_id: serverId,
    remote_path: remotePath,
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : "",
  };
}

export function normalizePlacementInspection(raw: unknown): PlacementInspection | null {
  if (!isRecord(raw)) return null;
  const placementId = str(raw.placement_id);
  if (placementId === null) return null;
  return {
    placement_id: placementId,
    state: inspectionState(raw.state),
    file_type: optionalStr(raw.file_type),
    size_b: intOrNull(raw.size_b),
    file_count: intOrNull(raw.file_count),
    checked_at: typeof raw.checked_at === "string" ? raw.checked_at : "",
    detail: typeof raw.detail === "string" ? raw.detail : "",
  };
}

export function normalizeLaunchConfigRecord(raw: unknown): LaunchConfigRecord | null {
  if (!isRecord(raw)) return null;
  const launchConfigId = str(raw.launch_config_id);
  const projectId = str(raw.project_id);
  const name = str(raw.name);
  const program = str(raw.program);
  if (launchConfigId === null || projectId === null || name === null || program === null) {
    return null;
  }
  return {
    launch_config_id: launchConfigId,
    project_id: projectId,
    name,
    working_dir: optionalStr(raw.working_dir),
    program,
    args: strList(raw.args),
    environment: optionalStr(raw.environment),
    env_vars: strRecord(raw.env_vars),
    required_artifact_ids: strList(raw.required_artifact_ids),
    gpu_count: intOrNull(raw.gpu_count),
    min_vram_b: intOrNull(raw.min_vram_b),
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : "",
  };
}

export function normalizeServerRoots(raw: unknown): ServerRoots {
  if (!isRecord(raw)) {
    return { project_root: null, dataset_root: null, model_root: null, output_root: null };
  }
  return {
    project_root: optionalStr(raw.project_root),
    dataset_root: optionalStr(raw.dataset_root),
    model_root: optionalStr(raw.model_root),
    output_root: optionalStr(raw.output_root),
  };
}

// -- Phase 4E: distribution snapshot + project sync plan -----------------------

export function normalizeDistributionItem(raw: unknown): DistributionItem | null {
  if (!isRecord(raw)) return null;
  const artifactId = str(raw.artifact_id);
  const serverId = str(raw.server_id);
  const remotePath = str(raw.remote_path);
  if (artifactId === null || serverId === null || remotePath === null) return null;
  return {
    artifact_id: artifactId,
    artifact_label: typeof raw.artifact_label === "string" ? raw.artifact_label : "",
    artifact_kind: typeof raw.artifact_kind === "string" ? raw.artifact_kind : "code",
    server_id: serverId,
    placement_id: optionalStr(raw.placement_id),
    remote_path: remotePath,
    state: distributionState(raw.state),
    checked_at: optionalStr(raw.checked_at),
    detail: optionalStr(raw.detail),
    active_transfer_job_id: optionalStr(raw.active_transfer_job_id),
  };
}

export function normalizeProjectDistribution(raw: unknown): ProjectDistribution | null {
  if (!isRecord(raw)) return null;
  const projectId = str(raw.project_id);
  if (projectId === null) return null;
  return {
    project_id: projectId,
    generated_at: typeof raw.generated_at === "string" ? raw.generated_at : "",
    items: Array.isArray(raw.items)
      ? raw.items
          .map(normalizeDistributionItem)
          .filter((item): item is DistributionItem => item !== null)
      : [],
  };
}

export function normalizeArtifactSyncItem(raw: unknown): ArtifactSyncItem | null {
  if (!isRecord(raw)) return null;
  const artifactId = str(raw.artifact_id);
  if (artifactId === null) return null;
  return {
    artifact_id: artifactId,
    artifact_label: typeof raw.artifact_label === "string" ? raw.artifact_label : "",
    artifact_kind: typeof raw.artifact_kind === "string" ? raw.artifact_kind : "code",
    target_status: distributionState(raw.target_status),
    action: syncAction(raw.action),
    reason: typeof raw.reason === "string" ? raw.reason : "",
    source_placement_id: optionalStr(raw.source_placement_id),
    source_server_id: optionalStr(raw.source_server_id),
    source_path: optionalStr(raw.source_path),
    target_path: optionalStr(raw.target_path),
    strategy_selected: strategyOrNull(raw.strategy_selected),
    alternatives: strList(raw.alternatives),
    warnings: strList(raw.warnings),
  };
}

export function normalizeProjectSyncPlan(raw: unknown): ProjectSyncPlan | null {
  if (!isRecord(raw)) return null;
  const projectId = str(raw.project_id);
  const targetServerId = str(raw.target_server_id);
  if (projectId === null || targetServerId === null) return null;
  return {
    project_id: projectId,
    target_server_id: targetServerId,
    generated_at: typeof raw.generated_at === "string" ? raw.generated_at : "",
    refresh_code: raw.refresh_code === true,
    items: Array.isArray(raw.items)
      ? raw.items
          .map(normalizeArtifactSyncItem)
          .filter((item): item is ArtifactSyncItem => item !== null)
      : [],
    valid: raw.valid !== false,
    error: typeof raw.error === "string" ? raw.error : "",
  };
}

// -- transport ----------------------------------------------------------------

type Method = "GET" | "POST" | "PATCH" | "PUT" | "DELETE";

function detailFromErrorBody(raw: unknown, fallback: string): string {
  if (!isRecord(raw)) return fallback;
  if (typeof raw.message === "string" && raw.message !== "") return raw.message;
  if (typeof raw.detail === "string" && raw.detail !== "") return raw.detail;
  if (Array.isArray(raw.detail)) {
    // pydantic 422: [{loc, msg, ...}, ...] — short machine-readable summary
    const first = raw.detail[0];
    if (isRecord(first) && typeof first.msg === "string") {
      const loc = Array.isArray(first.loc) ? first.loc.join(".") : "";
      return loc !== "" ? `${loc}: ${first.msg}` : first.msg;
    }
    return JSON.stringify(raw.detail);
  }
  return fallback;
}

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
    const fallback = `${response.status} ${response.statusText}`.trim();
    let detail = fallback;
    try {
      detail = detailFromErrorBody(await response.json(), fallback);
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

const enc = encodeURIComponent;

// -- client -------------------------------------------------------------------

export const workspaceApi = {
  async listProjects(): Promise<ProjectRecord[]> {
    const raw = await request("/workspace/projects", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("projects");
    return raw.map(normalizeProjectRecord).filter((p): p is ProjectRecord => p !== null);
  },

  async createProject(body: ProjectCreate): Promise<ProjectRecord> {
    const raw = await request("/workspace/projects", "POST", body);
    const record = normalizeProjectRecord(raw);
    if (record === null) throw invalidPayload("project");
    return record;
  },

  async patchProject(projectId: string, patch: ProjectPatch): Promise<ProjectRecord> {
    const raw = await request(`/workspace/projects/${enc(projectId)}`, "PATCH", patch);
    const record = normalizeProjectRecord(raw);
    if (record === null) throw invalidPayload("project");
    return record;
  },

  async deleteProject(projectId: string): Promise<void> {
    await request(`/workspace/projects/${enc(projectId)}`, "DELETE");
  },

  async listArtifacts(kind?: ArtifactKind): Promise<ArtifactRecord[]> {
    const query = kind === undefined ? "" : `?kind=${enc(kind)}`;
    const raw = await request(`/workspace/artifacts${query}`, "GET");
    if (!Array.isArray(raw)) throw invalidPayload("artifacts");
    return raw.map(normalizeArtifactRecord).filter((a): a is ArtifactRecord => a !== null);
  },

  async createArtifact(body: ArtifactCreate): Promise<ArtifactRecord> {
    const raw = await request("/workspace/artifacts", "POST", body);
    const record = normalizeArtifactRecord(raw);
    if (record === null) throw invalidPayload("artifact");
    return record;
  },

  async patchArtifact(artifactId: string, patch: ArtifactPatch): Promise<ArtifactRecord> {
    const raw = await request(`/workspace/artifacts/${enc(artifactId)}`, "PATCH", patch);
    const record = normalizeArtifactRecord(raw);
    if (record === null) throw invalidPayload("artifact");
    return record;
  },

  async deleteArtifact(artifactId: string): Promise<void> {
    await request(`/workspace/artifacts/${enc(artifactId)}`, "DELETE");
  },

  async listPlacements(): Promise<PlacementRecord[]> {
    const raw = await request("/workspace/placements", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("placements");
    return raw.map(normalizePlacementRecord).filter((p): p is PlacementRecord => p !== null);
  },

  async createPlacement(body: PlacementCreate): Promise<PlacementRecord> {
    const raw = await request("/workspace/placements", "POST", body);
    const record = normalizePlacementRecord(raw);
    if (record === null) throw invalidPayload("placement");
    return record;
  },

  async patchPlacement(placementId: string, patch: PlacementPatch): Promise<PlacementRecord> {
    const raw = await request(`/workspace/placements/${enc(placementId)}`, "PATCH", patch);
    const record = normalizePlacementRecord(raw);
    if (record === null) throw invalidPayload("placement");
    return record;
  },

  async deletePlacement(placementId: string): Promise<void> {
    await request(`/workspace/placements/${enc(placementId)}`, "DELETE");
  },

  /** Explicit SSH check on user request; results are RAM-only on both sides. */
  async inspectPlacement(placementId: string): Promise<PlacementInspection> {
    const raw = await request(`/workspace/placements/${enc(placementId)}/inspect`, "POST", {});
    const inspection = normalizePlacementInspection(raw);
    if (inspection === null) throw invalidPayload("placement inspection");
    return inspection;
  },

  async listLaunchConfigs(): Promise<LaunchConfigRecord[]> {
    const raw = await request("/workspace/launch-configs", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("launch configs");
    return raw.map(normalizeLaunchConfigRecord).filter((l): l is LaunchConfigRecord => l !== null);
  },

  async createLaunchConfig(body: LaunchConfigCreate): Promise<LaunchConfigRecord> {
    const raw = await request("/workspace/launch-configs", "POST", body);
    const record = normalizeLaunchConfigRecord(raw);
    if (record === null) throw invalidPayload("launch config");
    return record;
  },

  async patchLaunchConfig(
    launchConfigId: string,
    patch: LaunchConfigPatch,
  ): Promise<LaunchConfigRecord> {
    const raw = await request(`/workspace/launch-configs/${enc(launchConfigId)}`, "PATCH", patch);
    const record = normalizeLaunchConfigRecord(raw);
    if (record === null) throw invalidPayload("launch config");
    return record;
  },

  async deleteLaunchConfig(launchConfigId: string): Promise<void> {
    await request(`/workspace/launch-configs/${enc(launchConfigId)}`, "DELETE");
  },

  async getServerRoots(serverId: string): Promise<ServerRoots> {
    const raw = await request(`/workspace/server-roots/${enc(serverId)}`, "GET");
    return normalizeServerRoots(raw);
  },

  async putServerRoots(serverId: string, update: ServerRootsUpdate): Promise<ServerRoots> {
    const raw = await request(`/workspace/server-roots/${enc(serverId)}`, "PUT", update);
    return normalizeServerRoots(raw);
  },

  /** Zero-SSH read model: declarations + cached observations + active jobs. */
  async getProjectDistribution(projectId: string): Promise<ProjectDistribution> {
    const raw = await request(`/workspace/projects/${enc(projectId)}/distribution`, "GET");
    const distribution = normalizeProjectDistribution(raw);
    if (distribution === null) throw invalidPayload("project distribution");
    return distribution;
  },

  /** Explicit SSH check across the project's placements; returns a fresh snapshot. */
  async inspectProject(projectId: string): Promise<ProjectDistribution> {
    const raw = await request(`/workspace/projects/${enc(projectId)}/inspect`, "POST", {});
    const distribution = normalizeProjectDistribution(raw);
    if (distribution === null) throw invalidPayload("project distribution");
    return distribution;
  },

  /** Project-level reconciliation plan toward one target server (bounded SSH). */
  async buildSyncPlan(projectId: string, body: SyncPlanRequest): Promise<ProjectSyncPlan> {
    const raw = await request(`/workspace/projects/${enc(projectId)}/sync-plan`, "POST", body);
    const plan = normalizeProjectSyncPlan(raw);
    if (plan === null) throw invalidPayload("project sync plan");
    return plan;
  },

  /** Execute the sync; the backend regenerates the plan and 409s on UNRESOLVED. */
  async syncProject(projectId: string, body: SyncPlanRequest): Promise<TransferBatch> {
    const raw = await request(`/workspace/projects/${enc(projectId)}/sync`, "POST", body);
    const batch = normalizeTransferBatch(raw);
    if (batch === null) throw invalidPayload("transfer batch");
    return batch;
  },
};

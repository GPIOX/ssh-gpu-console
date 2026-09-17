/**
 * Typed REST client for cross-server transfers (/api/v1/transfers).
 * Mirrors services/workspaceApi.ts patterns: one request helper, defensive
 * normalization, ApiError with status + human detail. Listing is RAM-only on
 * the backend and never triggers SSH; progress is polled by the store.
 */

import { API_BASE, ApiError } from "./api";
import { isRecord } from "./normalize";
import type {
  TransferBatch,
  TransferBatchState,
  TransferJob,
  TransferPlan,
  TransferRequest,
  TransferState,
  TransferStrategy,
} from "../types/transfers";

const STRATEGIES = ["auto", "direct_rsync", "local_relay"] as const;

export function transferStrategy(value: unknown, fallback: TransferStrategy = "auto"): TransferStrategy {
  return typeof value === "string" && (STRATEGIES as readonly string[]).includes(value)
    ? (value as TransferStrategy)
    : fallback;
}

const STATES = [
  "queued",
  "planning",
  "running",
  "verifying",
  "completed",
  "failed",
  "cancelled",
] as const;

function transferState(value: unknown): TransferState {
  return typeof value === "string" && (STATES as readonly string[]).includes(value)
    ? (value as TransferState)
    : "queued";
}

const BATCH_STATES = [
  "queued",
  "running",
  "completed",
  "partial_failed",
  "failed",
  "cancelled",
] as const;

function batchState(value: unknown): TransferBatchState {
  return typeof value === "string" && (BATCH_STATES as readonly string[]).includes(value)
    ? (value as TransferBatchState)
    : "queued";
}

// -- primitive guards (same vocabulary as workspaceApi) -----------------------

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function optionalStr(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function intOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function numOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function finiteOrZero(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function strList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function boolRecord(value: unknown): Record<string, boolean> {
  if (!isRecord(value)) return {};
  const out: Record<string, boolean> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "boolean") out[key] = item;
  }
  return out;
}

// -- normalizers ---------------------------------------------------------------

export function normalizeTransferJob(raw: unknown): TransferJob | null {
  if (!isRecord(raw)) return null;
  const jobId = str(raw.job_id);
  const artifactId = str(raw.artifact_id);
  const sourceServerId = str(raw.source_server_id);
  const sourcePath = str(raw.source_path);
  const targetServerId = str(raw.target_server_id);
  const targetPath = str(raw.target_path);
  if (
    jobId === null ||
    artifactId === null ||
    sourceServerId === null ||
    sourcePath === null ||
    targetServerId === null ||
    targetPath === null
  ) {
    return null;
  }
  return {
    job_id: jobId,
    artifact_id: artifactId,
    artifact_label: typeof raw.artifact_label === "string" ? raw.artifact_label : "",
    source_server_id: sourceServerId,
    source_path: sourcePath,
    target_server_id: targetServerId,
    target_path: targetPath,
    strategy_requested: transferStrategy(raw.strategy_requested, "auto"),
    strategy_used: raw.strategy_used === null ? null : transferStrategy(raw.strategy_used, "auto"),
    state: transferState(raw.state),
    excludes: strList(raw.excludes),
    immutable: raw.immutable === true,
    strategy_reason: typeof raw.strategy_reason === "string" ? raw.strategy_reason : "",
    resumed_bytes: finiteOrZero(raw.resumed_bytes),
    files_skipped: finiteOrZero(raw.files_skipped),
    bytes_skipped: finiteOrZero(raw.bytes_skipped),
    warnings: strList(raw.warnings),
    bytes_total: intOrNull(raw.bytes_total),
    bytes_done: typeof raw.bytes_done === "number" && Number.isFinite(raw.bytes_done) ? raw.bytes_done : 0,
    files_total: intOrNull(raw.files_total),
    files_done: typeof raw.files_done === "number" && Number.isFinite(raw.files_done) ? raw.files_done : 0,
    rate_bps: numOrNull(raw.rate_bps),
    eta_s: numOrNull(raw.eta_s),
    current_path: optionalStr(raw.current_path),
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    started_at: optionalStr(raw.started_at),
    finished_at: optionalStr(raw.finished_at),
    error_code: optionalStr(raw.error_code),
    error_message: typeof raw.error_message === "string" ? raw.error_message : "",
  };
}

export function normalizeTransferPlan(raw: unknown): TransferPlan | null {
  if (!isRecord(raw)) return null;
  const artifactId = str(raw.artifact_id);
  const sourceServerId = str(raw.source_server_id);
  const sourcePath = str(raw.source_path);
  const targetServerId = str(raw.target_server_id);
  const targetPath = str(raw.target_path);
  if (
    artifactId === null ||
    sourceServerId === null ||
    sourcePath === null ||
    targetServerId === null ||
    targetPath === null
  ) {
    return null;
  }
  return {
    artifact_id: artifactId,
    source_server_id: sourceServerId,
    source_path: sourcePath,
    target_server_id: targetServerId,
    target_path: targetPath,
    strategy_requested: transferStrategy(raw.strategy_requested, "auto"),
    strategy_available: boolRecord(raw.strategy_available),
    strategy_selected: raw.strategy_selected === null ? null : transferStrategy(raw.strategy_selected, "auto"),
    reason: typeof raw.reason === "string" ? raw.reason : "",
    excludes: strList(raw.excludes),
    source_exists: typeof raw.source_exists === "boolean" ? raw.source_exists : null,
    source_size_b: intOrNull(raw.source_size_b),
    target_free_b: intOrNull(raw.target_free_b),
    space_warning: typeof raw.space_warning === "string" ? raw.space_warning : "",
  };
}

/** Phase 4E: batch grouping record; counts derive from real jobs server-side. */
export function normalizeTransferBatch(raw: unknown): TransferBatch | null {
  if (!isRecord(raw)) return null;
  const batchId = str(raw.batch_id);
  const projectId = str(raw.project_id);
  const targetServerId = str(raw.target_server_id);
  if (batchId === null || projectId === null || targetServerId === null) return null;
  return {
    batch_id: batchId,
    project_id: projectId,
    target_server_id: targetServerId,
    job_ids: strList(raw.job_ids),
    created_at: typeof raw.created_at === "string" ? raw.created_at : "",
    state: batchState(raw.state),
    total_jobs: finiteOrZero(raw.total_jobs),
    queued_jobs: finiteOrZero(raw.queued_jobs),
    running_jobs: finiteOrZero(raw.running_jobs),
    completed_jobs: finiteOrZero(raw.completed_jobs),
    failed_jobs: finiteOrZero(raw.failed_jobs),
    cancelled_jobs: finiteOrZero(raw.cancelled_jobs),
  };
}

// -- transport -----------------------------------------------------------------

type Method = "GET" | "POST" | "DELETE";

function detailFromErrorBody(raw: unknown, fallback: string): string {
  if (!isRecord(raw)) return fallback;
  if (typeof raw.message === "string" && raw.message !== "") return raw.message;
  if (typeof raw.detail === "string" && raw.detail !== "") return raw.detail;
  if (Array.isArray(raw.detail)) {
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

// -- client ----------------------------------------------------------------------

export const transfersApi = {
  /** RAM-only listing; never triggers SSH on the backend. */
  async listJobs(): Promise<TransferJob[]> {
    const raw = await request("/transfers", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("transfers");
    return raw.map(normalizeTransferJob).filter((job): job is TransferJob => job !== null);
  },

  async getJob(jobId: string): Promise<TransferJob> {
    const raw = await request(`/transfers/${enc(jobId)}`, "GET");
    const job = normalizeTransferJob(raw);
    if (job === null) throw invalidPayload("transfer job");
    return job;
  },

  /** Planning runs the direct-rsync preflight (explicit SSH); no job created. */
  async plan(requestBody: TransferRequest): Promise<TransferPlan> {
    const raw = await request("/transfers/plan", "POST", requestBody);
    const plan = normalizeTransferPlan(raw);
    if (plan === null) throw invalidPayload("transfer plan");
    return plan;
  },

  /** Queue a job; returns 202 {job_id}. */
  async create(requestBody: TransferRequest): Promise<string> {
    const raw = await request("/transfers", "POST", requestBody);
    const jobId = isRecord(raw) ? str(raw.job_id) : null;
    if (jobId === null) throw invalidPayload("transfer job id");
    return jobId;
  },

  /** Ask the worker to cancel; job moves to cancelling/failed/cancelled async. */
  async cancel(jobId: string): Promise<void> {
    await request(`/transfers/${enc(jobId)}/cancel`, "POST", {});
  },

  /** Re-queue a failed/completed/cancelled job; returns 202 {job_id}. */
  async retry(jobId: string): Promise<string> {
    const raw = await request(`/transfers/${enc(jobId)}/retry`, "POST", {});
    const jobIdOut = isRecord(raw) ? str(raw.job_id) : null;
    if (jobIdOut === null) throw invalidPayload("transfer job id");
    return jobIdOut;
  },

  /** Drop terminal (completed/failed/cancelled) jobs from RAM history; active jobs are untouched. Returns {"cleared": n}. */
  async clearHistory(): Promise<number> {
    const raw = await request("/transfers", "DELETE");
    const cleared = isRecord(raw) ? intOrNull(raw.cleared) : null;
    if (cleared === null) throw invalidPayload("cleared count");
    return cleared;
  },

  /** RAM-only batch listing (active + archived, newest first); zero SSH. */
  async listBatches(): Promise<TransferBatch[]> {
    const raw = await request("/transfer-batches", "GET");
    if (!Array.isArray(raw)) throw invalidPayload("transfer batches");
    return raw.map(normalizeTransferBatch).filter((batch): batch is TransferBatch => batch !== null);
  },

  async getBatch(batchId: string): Promise<TransferBatch> {
    const raw = await request(`/transfer-batches/${enc(batchId)}`, "GET");
    const batch = normalizeTransferBatch(raw);
    if (batch === null) throw invalidPayload("transfer batch");
    return batch;
  },
};

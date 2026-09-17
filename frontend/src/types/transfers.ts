/**
 * Transfer wire contracts — mirror backend/app/models/transfer.py exactly
 * (separate module: never extend types/models.ts or types/workspace.ts).
 * TransferJob is a runtime object: RAM only on both sides, never persisted.
 */

export type TransferStrategy = "auto" | "direct_rsync" | "local_relay";

/** queued → planning → running → verifying → completed | failed | cancelled */
export type TransferState =
  | "queued"
  | "planning"
  | "running"
  | "verifying"
  | "completed"
  | "failed"
  | "cancelled";

/** Only "quick" exists this phase (size / byte count / basic file count). */
export type VerifyMode = "quick";

/** States the frontend must poll for (~1 Hz) while any job is in one. */
export const ACTIVE_TRANSFER_STATES: readonly TransferState[] = [
  "queued",
  "planning",
  "running",
  "verifying",
];

export function isActiveTransferState(state: TransferState): boolean {
  return ACTIVE_TRANSFER_STATES.includes(state);
}

/** Row-level transfer kind shown next to the strategy chip. */
export type TransferKind = "fresh" | "resumed" | "incremental";

/**
 * "incremental" wins when both apply: a resumed-partial is subsumed by the
 * incremental-sync semantics. A fresh transfer renders no chip at all.
 */
export function transferKind(
  job: Pick<TransferJob, "resumed_bytes" | "bytes_skipped">,
): TransferKind {
  if (job.bytes_skipped > 0) return "incremental";
  if (job.resumed_bytes > 0) return "resumed";
  return "fresh";
}

export interface TransferPlan {
  artifact_id: string;
  source_server_id: string;
  source_path: string;
  target_server_id: string;
  target_path: string;
  strategy_requested: TransferStrategy;
  /** direct_rsync / local_relay availability reported by the planner. */
  strategy_available: Record<string, boolean>;
  /** null when the requested strategy is unavailable (auto never yields null). */
  strategy_selected: TransferStrategy | null;
  reason: string;
  source_exists: boolean | null;
  source_size_b: number | null;
  /** Effective exclusions (union of referencing projects' defaults). */
  excludes: string[];
  /** Preflight target disk probe; null = not probed / unknown. */
  target_free_b: number | null;
  /** "Not enough space" warning text for the plan preview; "" = none. */
  space_warning: string;
}

export interface TransferRequest {
  artifact_id: string;
  source_placement_id: string;
  target_server_id: string;
  target_path: string;
  strategy: TransferStrategy;
  verify_mode: VerifyMode;
}

export interface TransferJob {
  job_id: string;
  artifact_id: string;
  /** "name:version" display label resolved server-side. */
  artifact_label: string;

  source_server_id: string;
  source_path: string;
  target_server_id: string;
  target_path: string;

  strategy_requested: TransferStrategy;
  strategy_used: TransferStrategy | null;
  state: TransferState;
  excludes: string[];

  /** Phase 3 run bookkeeping (all defaulted server-side). */
  /** dataset/model: the target must not be mutated; code artifacts may. */
  immutable: boolean;
  /** Copy of plan.reason for job detail views. */
  strategy_reason: string;
  /** Bytes carried over from an existing partial file at job start. */
  resumed_bytes: number;
  /** Incremental sync: files/bytes already up-to-date on the target. */
  files_skipped: number;
  bytes_skipped: number;
  /** Non-fatal runner notes, e.g. skipped symlinks (≤20 entries). */
  warnings: string[];

  bytes_total: number | null;
  bytes_done: number;
  files_total: number | null;
  files_done: number;
  rate_bps: number | null;
  eta_s: number | null;
  current_path: string | null;

  created_at: string;
  started_at: string | null;
  finished_at: string | null;

  error_code: string | null;
  error_message: string;
}

export interface TransferPrefill {
  artifactId?: string;
  sourcePlacementId?: string;
}

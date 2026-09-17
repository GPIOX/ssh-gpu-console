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

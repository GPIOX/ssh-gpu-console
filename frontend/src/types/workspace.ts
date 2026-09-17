/**
 * Workspace wire contracts — mirror backend/app/models/workspace.py exactly
 * (separate module: never extend types/models.ts). All records are persistent
 * declarations written only on explicit CRUD; PlacementInspection is a runtime
 * observation that lives in RAM on both sides and is never persisted.
 */

export type ArtifactKind = "code" | "dataset" | "model";

export type InspectionState = "declared" | "verified" | "missing" | "unavailable";

export interface ProjectRecord {
  project_id: string;
  name: string;
  description: string;
  artifact_ids: string[];
  launch_config_ids: string[];
  tags: string[];
  /** Default transfer exclusion patterns (rsync/fnmatch) for this project. */
  transfer_excludes: string[];
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  description?: string;
  artifact_ids?: string[];
  tags?: string[];
}

export interface ProjectPatch {
  name?: string;
  description?: string;
  artifact_ids?: string[];
  tags?: string[];
  transfer_excludes?: string[];
}

export interface ArtifactRecord {
  artifact_id: string;
  kind: ArtifactKind;
  name: string;
  /** null = unversioned; content changes create a new version instead. */
  version: string | null;
  description: string;
  immutable: boolean;
  created_at: string;
  updated_at: string;
}

export interface ArtifactCreate {
  kind: ArtifactKind;
  name: string;
  version?: string | null;
  description?: string;
  /** Omitted → backend default: immutable unless kind is code. */
  immutable?: boolean | null;
}

export interface ArtifactPatch {
  name?: string;
  version?: string | null;
  description?: string;
  immutable?: boolean | null;
}

export interface PlacementRecord {
  placement_id: string;
  artifact_id: string;
  server_id: string;
  remote_path: string;
  created_at: string;
  updated_at: string;
}

export interface PlacementCreate {
  artifact_id: string;
  server_id: string;
  remote_path: string;
}

export interface PlacementPatch {
  remote_path?: string;
}

/** RAM-only scan result; `checked_at` marks when the explicit inspect ran. */
export interface PlacementInspection {
  placement_id: string;
  state: InspectionState;
  file_type: string | null;
  size_b: number | null;
  file_count: number | null;
  checked_at: string;
  detail: string;
}

export interface LaunchConfigRecord {
  launch_config_id: string;
  project_id: string;
  name: string;
  working_dir: string | null;
  /** Structured launch: program + args[], never a shell string. */
  program: string;
  args: string[];
  environment: string | null;
  env_vars: Record<string, string>;
  required_artifact_ids: string[];
  gpu_count: number | null;
  min_vram_b: number | null;
  created_at: string;
  updated_at: string;
}

export interface LaunchConfigCreate {
  project_id: string;
  name: string;
  working_dir?: string | null;
  program: string;
  args?: string[];
  environment?: string | null;
  env_vars?: Record<string, string>;
  required_artifact_ids?: string[];
  gpu_count?: number | null;
  min_vram_b?: number | null;
}

export interface LaunchConfigPatch {
  name?: string;
  working_dir?: string | null;
  program?: string;
  args?: string[];
  environment?: string | null;
  env_vars?: Record<string, string>;
  required_artifact_ids?: string[];
  gpu_count?: number | null;
  min_vram_b?: number | null;
}

export interface ServerRoots {
  project_root: string | null;
  dataset_root: string | null;
  model_root: string | null;
  output_root: string | null;
}

/** PUT replaces the whole record: omit → null (cleared). Fields are 1–512
 *  chars when present, so an empty input is sent as null, never "". */
export interface ServerRootsUpdate {
  project_root?: string | null;
  dataset_root?: string | null;
  model_root?: string | null;
  output_root?: string | null;
}

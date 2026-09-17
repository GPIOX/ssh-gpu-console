/**
 * Wire contracts — mirror backend/app/models/{server,telemetry}.py exactly
 * (Pydantic extra="forbid" models, camel-free snake_case on the wire).
 *
 * Semantics (binding, from telemetry.py):
 * - `null` means "not reported by the remote host" — render N/A, never fabricate;
 * - bytes fields are ints and suffixed `_b`; percentages are floats 0..100;
 * - timestamps arrive as ISO-8601 strings (Pydantic datetime serialization).
 */

// ---------------------------------------------------------------------------
// Server registry (backend/app/models/server.py)
// ---------------------------------------------------------------------------

export type ServerStatus =
  | "unknown"
  | "connecting"
  | "online"
  | "reconnecting"
  | "offline"
  | "timeout"
  | "authentication_failed"
  | "host_key_error"
  | "degraded";

export interface ServerRecord {
  server_id: string;
  display_name: string;
  /** alias or hostname */
  ssh_host: string;
  username: string | null;
  port: number | null;
  tags: string[];
  enabled: boolean;
  /** Per-server FAST-cadence override in seconds (0.5–600); null = global
   *  default (2.5 s). Optional on the wire because fixtures/older payloads
   *  may omit it; consumers must treat missing as null. */
  sample_interval_s?: number | null;
}

export interface ServerCreate {
  display_name: string;
  ssh_host: string;
  username?: string | null;
  port?: number | null;
  tags?: string[];
  enabled?: boolean;
  /** Omit to use the global default; when provided must be 0.5–600. */
  sample_interval_s?: number | null;
}

export interface ServerPatch {
  display_name?: string | null;
  ssh_host?: string | null;
  username?: string | null;
  port?: number | null;
  tags?: string[] | null;
  enabled?: boolean | null;
  /** Only present when the user set a value; omitted (not null) when cleared,
   *  since the wire PATCH cannot distinguish null from "not provided". */
  sample_interval_s?: number | null;
}

/** One usable host entry parsed from the user's ~/.ssh/config. */
export interface AliasEntry {
  alias: string;
  host: string | null;
  user: string | null;
  port: number | null;
}

/** An untrusted host key surfaced for explicit user consent (TOFU). */
export interface HostKeyPrompt {
  host: string;
  port: number | null;
  key_type: string;
  /** sha256 format, e.g. "SHA256:...." */
  fingerprint: string;
}

/** Outcome of an explicit connection test for onboarding. */
export interface ConnectionTestResult {
  ok: boolean;
  /** ServerStatus value; on failure one of the failure states */
  status: ServerStatus;
  detail: string;
  latency_ms: number | null;
  pending_host_key: HostKeyPrompt | null;
}

// ---------------------------------------------------------------------------
// Telemetry (backend/app/models/telemetry.py)
// ---------------------------------------------------------------------------

export type GpuAvailability = "free" | "active" | "saturated" | "unavailable";

export interface CpuInfo {
  percent: number | null;
  load_1: number | null;
  load_5: number | null;
  load_15: number | null;
  cores_logical: number | null;
}

export interface MemoryInfo {
  total_b: number | null;
  used_b: number | null;
  available_b: number | null;
  percent: number | null;
}

export interface GpuInfo {
  index: number;
  uuid: string | null;
  name: string | null;
  utilization_percent: number | null;
  vram_used_b: number | null;
  vram_total_b: number | null;
  vram_percent: number | null;
  temperature_c: number | null;
  power_watts: number | null;
  power_limit_watts: number | null;
  fan_percent: number | null;
  availability: GpuAvailability;
  process_count: number;
}

export interface GpuProcessInfo {
  pid: number;
  gpu_uuid: string | null;
  gpu_index: number | null;
  used_memory_b: number | null;
  process_name: string | null;
  user: string | null;
  command: string | null;
}

export interface ProcessInfo {
  pid: number;
  user: string | null;
  name: string | null;
  cpu_percent: number | null;
  mem_percent: number | null;
  rss_b: number | null;
  state: string | null;
  command: string | null;
  gpu_indexes: number[];
  gpu_vram_b: number | null;
}

export interface StorageMount {
  device: string;
  fstype: string | null;
  mount: string;
  total_b: number | null;
  used_b: number | null;
  free_b: number | null;
  percent: number | null;
}

export interface NetworkInterfaceInfo {
  name: string;
  ip: string | null;
  rx_bps: number | null;
  tx_bps: number | null;
  rx_total_b: number | null;
  tx_total_b: number | null;
}

export interface SystemInfo {
  hostname: string | null;
  os_pretty: string | null;
  kernel: string | null;
  uptime_s: number | null;
  cpu_model: string | null;
  cores_physical: number | null;
  cores_logical: number | null;
  driver_version: string | null;
}

/** Full detail snapshot for one server. Sections may be absent/stale independently. */
export interface ServerSnapshot {
  server_id: string;
  status: ServerStatus;
  generated_at: string | null;
  cpu: CpuInfo | null;
  memory: MemoryInfo | null;
  gpus: GpuInfo[];
  gpu_processes: GpuProcessInfo[];
  processes: ProcessInfo[];
  storage: StorageMount[];
  network: NetworkInterfaceInfo[];
  system: SystemInfo | null;
  /** per-section error codes, e.g. {"gpu": "command_missing", "network": "timeout"} */
  errors: Record<string, string>;
  stale: boolean;
}

/** Compact per-GPU lane for the fleet view (no processes, no history). */
export interface FleetGpu {
  index: number;
  utilization_percent: number | null;
  vram_used_b: number | null;
  vram_total_b: number | null;
  temperature_c: number | null;
  /** Optional for fixture/test compatibility; consumers treat missing as null. */
  power_watts?: number | null;
  power_limit_watts?: number | null;
  users?: string[];
  availability: GpuAvailability;
}

/** Low-cost per-server summary for the fleet view. */
export interface FleetEntry {
  server_id: string;
  display_name: string;
  ssh_endpoint: string | null;
  status: ServerStatus;
  enabled: boolean;
  os_pretty: string | null;
  gpu_model: string | null;
  gpu_count: number;
  gpu_busy: number;
  gpu_free: number;
  cpu_percent: number | null;
  memory_percent: number | null;
  vram_used_b: number | null;
  vram_total_b: number | null;
  disk_warning: boolean;
  /** Mirrors backend `gpus: list[FleetGpu] = Field(default_factory=list)`.
   *  Optional on the wire contract because the current services/normalize.ts
   *  pass does not yet surface the field; consumers must treat missing as []. */
  gpus?: FleetGpu[];
  updated_at: string | null;
}

export interface FleetSummary {
  generated_at: string;
  servers: FleetEntry[];
}

export interface HistoryPoint {
  t: string;
  v: number;
}

// ---------------------------------------------------------------------------
// WebSocket /api/v1/realtime
// ---------------------------------------------------------------------------

/** server → client frames; anything else is malformed and must be ignored. */
export type ServerToClientFrame =
  | { type: "hello"; server_ids: string[] }
  | { type: "fleet"; summary: FleetSummary }
  | { type: "server"; server_id: string; snapshot: ServerSnapshot }
  | { type: "status"; server_id: string; status: ServerStatus };

/** client → server frames. */
export type ClientToServerFrame =
  | { type: "select"; server_id: string | null }
  | { type: "ping" };

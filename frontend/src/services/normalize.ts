/**
 * Defensive normalizers for wire payloads (REST bodies and WebSocket frames).
 * Wire data is untrusted: every normalizer returns a fully-shaped model or
 * null/defaults — it never throws and never fabricates telemetry values.
 */

import type {
  AliasEntry,
  ConnectionTestResult,
  CpuInfo,
  FleetEntry,
  FleetSummary,
  GpuAvailability,
  GpuInfo,
  GpuProcessInfo,
  HistoryPoint,
  HostKeyPrompt,
  MemoryInfo,
  NetworkInterfaceInfo,
  ProcessInfo,
  ServerRecord,
  ServerSnapshot,
  ServerStatus,
  StorageMount,
  SystemInfo,
  ServerToClientFrame,
  FleetGpu,
} from "../types/models";

// -- primitive guards -------------------------------------------------------

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/** Per-server fast-cadence override: finite seconds within the 0.5–600 bounds,
 *  else null (out-of-range or non-numeric values are ignored, never clamped). */
function interval(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0.5 && value <= 600
    ? value
    : null;
}

function arrayOf(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function strList(value: unknown): string[] {
  return arrayOf(value).filter((item): item is string => typeof item === "string");
}

function recordOf(value: unknown): Record<string, string> {
  if (!isRecord(value)) return {};
  const out: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item === "string") out[key] = item;
  }
  return out;
}

// -- enums ------------------------------------------------------------------

export const SERVER_STATUSES = [
  "unknown",
  "connecting",
  "online",
  "reconnecting",
  "offline",
  "timeout",
  "authentication_failed",
  "host_key_error",
  "degraded",
] as const;

export function serverStatus(value: unknown): ServerStatus {
  return typeof value === "string" && (SERVER_STATUSES as readonly string[]).includes(value)
    ? (value as ServerStatus)
    : "unknown";
}

const GPU_AVAILABILITIES = ["free", "active", "saturated", "unavailable"] as const;

export function gpuAvailability(value: unknown): GpuAvailability {
  return typeof value === "string" && (GPU_AVAILABILITIES as readonly string[]).includes(value)
    ? (value as GpuAvailability)
    : "unavailable";
}

// -- registry models ---------------------------------------------------------

export function normalizeServerRecord(raw: unknown): ServerRecord | null {
  if (!isRecord(raw)) return null;
  const serverId = str(raw.server_id);
  const displayName = str(raw.display_name);
  const sshHost = str(raw.ssh_host);
  if (serverId === null || displayName === null || sshHost === null) return null;
  return {
    server_id: serverId,
    display_name: displayName,
    ssh_host: sshHost,
    username: str(raw.username),
    port: int(raw.port),
    tags: strList(raw.tags),
    enabled: bool(raw.enabled, true),
    sample_interval_s: interval(raw.sample_interval_s),
  };
}

export function normalizeAliasEntry(raw: unknown): AliasEntry | null {
  if (!isRecord(raw)) return null;
  const alias = str(raw.alias);
  if (alias === null) return null;
  return {
    alias,
    host: str(raw.host),
    user: str(raw.user),
    port: int(raw.port),
  };
}

export function normalizeHostKeyPrompt(raw: unknown): HostKeyPrompt | null {
  if (!isRecord(raw)) return null;
  const host = str(raw.host);
  const keyType = str(raw.key_type);
  const fingerprint = str(raw.fingerprint);
  if (host === null || keyType === null || fingerprint === null) return null;
  return { host, port: int(raw.port), key_type: keyType, fingerprint };
}

export function normalizeConnectionTestResult(raw: unknown): ConnectionTestResult | null {
  if (!isRecord(raw)) return null;
  const ok = typeof raw.ok === "boolean" ? raw.ok : null;
  const detail = str(raw.detail);
  if (ok === null || detail === null) return null;
  return {
    ok,
    status: serverStatus(raw.status),
    detail,
    latency_ms: num(raw.latency_ms),
    pending_host_key: normalizeHostKeyPrompt(raw.pending_host_key),
  };
}

// -- telemetry models --------------------------------------------------------

function normalizeCpu(raw: unknown): CpuInfo | null {
  if (!isRecord(raw)) return null;
  return {
    percent: num(raw.percent),
    load_1: num(raw.load_1),
    load_5: num(raw.load_5),
    load_15: num(raw.load_15),
    cores_logical: int(raw.cores_logical),
  };
}

function normalizeMemory(raw: unknown): MemoryInfo | null {
  if (!isRecord(raw)) return null;
  return {
    total_b: int(raw.total_b),
    used_b: int(raw.used_b),
    available_b: int(raw.available_b),
    percent: num(raw.percent),
  };
}

function normalizeGpu(raw: unknown): GpuInfo | null {
  if (!isRecord(raw)) return null;
  const index = int(raw.index);
  if (index === null) return null;
  return {
    index,
    uuid: str(raw.uuid),
    name: str(raw.name),
    utilization_percent: num(raw.utilization_percent),
    vram_used_b: int(raw.vram_used_b),
    vram_total_b: int(raw.vram_total_b),
    vram_percent: num(raw.vram_percent),
    temperature_c: num(raw.temperature_c),
    power_watts: num(raw.power_watts),
    power_limit_watts: num(raw.power_limit_watts),
    fan_percent: num(raw.fan_percent),
    availability: gpuAvailability(raw.availability),
    process_count: int(raw.process_count) ?? 0,
  };
}

function normalizeGpuProcess(raw: unknown): GpuProcessInfo | null {
  if (!isRecord(raw)) return null;
  const pid = int(raw.pid);
  if (pid === null) return null;
  return {
    pid,
    gpu_uuid: str(raw.gpu_uuid),
    gpu_index: int(raw.gpu_index),
    used_memory_b: int(raw.used_memory_b),
    process_name: str(raw.process_name),
    user: str(raw.user),
    command: str(raw.command),
  };
}

function normalizeProcess(raw: unknown): ProcessInfo | null {
  if (!isRecord(raw)) return null;
  const pid = int(raw.pid);
  if (pid === null) return null;
  return {
    pid,
    user: str(raw.user),
    name: str(raw.name),
    cpu_percent: num(raw.cpu_percent),
    mem_percent: num(raw.mem_percent),
    rss_b: int(raw.rss_b),
    state: str(raw.state),
    command: str(raw.command),
    gpu_indexes: arrayOf(raw.gpu_indexes).filter(
      (item): item is number => typeof item === "number" && Number.isInteger(item),
    ),
    gpu_vram_b: int(raw.gpu_vram_b),
  };
}

function normalizeStorageMount(raw: unknown): StorageMount | null {
  if (!isRecord(raw)) return null;
  const device = str(raw.device);
  const mount = str(raw.mount);
  if (device === null || mount === null) return null;
  return {
    device,
    fstype: str(raw.fstype),
    mount,
    total_b: int(raw.total_b),
    used_b: int(raw.used_b),
    free_b: int(raw.free_b),
    percent: num(raw.percent),
  };
}

function normalizeNetworkInterface(raw: unknown): NetworkInterfaceInfo | null {
  if (!isRecord(raw)) return null;
  const name = str(raw.name);
  if (name === null) return null;
  return {
    name,
    ip: str(raw.ip),
    rx_bps: num(raw.rx_bps),
    tx_bps: num(raw.tx_bps),
    rx_total_b: int(raw.rx_total_b),
    tx_total_b: int(raw.tx_total_b),
  };
}

function normalizeSystem(raw: unknown): SystemInfo | null {
  if (!isRecord(raw)) return null;
  return {
    hostname: str(raw.hostname),
    os_pretty: str(raw.os_pretty),
    kernel: str(raw.kernel),
    uptime_s: num(raw.uptime_s),
    cpu_model: str(raw.cpu_model),
    cores_physical: int(raw.cores_physical),
    cores_logical: int(raw.cores_logical),
    driver_version: str(raw.driver_version),
  };
}

export function normalizeServerSnapshot(raw: unknown): ServerSnapshot | null {
  if (!isRecord(raw)) return null;
  const serverId = str(raw.server_id);
  if (serverId === null) return null;
  return {
    server_id: serverId,
    status: serverStatus(raw.status),
    generated_at: str(raw.generated_at),
    cpu: normalizeCpu(raw.cpu),
    memory: normalizeMemory(raw.memory),
    gpus: arrayOf(raw.gpus)
      .map(normalizeGpu)
      .filter((gpu): gpu is GpuInfo => gpu !== null),
    gpu_processes: arrayOf(raw.gpu_processes)
      .map(normalizeGpuProcess)
      .filter((p): p is GpuProcessInfo => p !== null),
    processes: arrayOf(raw.processes)
      .map(normalizeProcess)
      .filter((p): p is ProcessInfo => p !== null),
    storage: arrayOf(raw.storage)
      .map(normalizeStorageMount)
      .filter((m): m is StorageMount => m !== null),
    network: arrayOf(raw.network)
      .map(normalizeNetworkInterface)
      .filter((n): n is NetworkInterfaceInfo => n !== null),
    system: normalizeSystem(raw.system),
    errors: recordOf(raw.errors),
    stale: bool(raw.stale, false),
  };
}

function normalizeFleetGpu(raw: unknown): FleetGpu | null {
  if (!isRecord(raw)) return null;
  const index = int(raw.index);
  if (index === null) return null;
  return {
    index,
    utilization_percent: num(raw.utilization_percent),
    vram_used_b: int(raw.vram_used_b),
    vram_total_b: int(raw.vram_total_b),
    temperature_c: num(raw.temperature_c),
    power_watts: num(raw.power_watts),
    power_limit_watts: num(raw.power_limit_watts),
    users: arrayOf(raw.users)
      .map((item): string | null => (typeof item === "string" && item !== "" ? item : null))
      .filter((u): u is string => u !== null),
    availability: gpuAvailability(raw.availability),
  };
}

export function normalizeFleetEntry(raw: unknown): FleetEntry | null {
  if (!isRecord(raw)) return null;
  const serverId = str(raw.server_id);
  const displayName = str(raw.display_name);
  if (serverId === null || displayName === null) return null;
  return {
    server_id: serverId,
    display_name: displayName,
    ssh_endpoint: str(raw.ssh_endpoint),
    status: serverStatus(raw.status),
    enabled: bool(raw.enabled, true),
    os_pretty: str(raw.os_pretty),
    gpu_model: str(raw.gpu_model),
    gpu_count: int(raw.gpu_count) ?? 0,
    gpu_busy: int(raw.gpu_busy) ?? 0,
    gpu_free: int(raw.gpu_free) ?? 0,
    cpu_percent: num(raw.cpu_percent),
    memory_percent: num(raw.memory_percent),
    vram_used_b: int(raw.vram_used_b),
    vram_total_b: int(raw.vram_total_b),
    disk_warning: bool(raw.disk_warning, false),
    gpus: arrayOf(raw.gpus)
      .map(normalizeFleetGpu)
      .filter((g): g is FleetGpu => g !== null),
    updated_at: str(raw.updated_at),
  };
}

export function normalizeFleetSummary(raw: unknown): FleetSummary | null {
  if (!isRecord(raw)) return null;
  const generatedAt = str(raw.generated_at);
  if (generatedAt === null) return null;
  return {
    generated_at: generatedAt,
    servers: arrayOf(raw.servers)
      .map(normalizeFleetEntry)
      .filter((entry): entry is FleetEntry => entry !== null),
  };
}

export function normalizeHistoryPoints(raw: unknown): HistoryPoint[] {
  return arrayOf(raw)
    .map((item): HistoryPoint | null => {
      if (!isRecord(item)) return null;
      const t = str(item.t);
      const v = num(item.v);
      if (t === null || v === null) return null;
      return { t, v };
    })
    .filter((point): point is HistoryPoint => point !== null);
}

// -- WebSocket frames ---------------------------------------------------------

/** Returns a typed frame, or null for malformed/unknown payloads (caller ignores). */
export function normalizeFrame(raw: unknown): ServerToClientFrame | null {
  if (!isRecord(raw)) return null;
  switch (raw.type) {
    case "hello":
      return { type: "hello", server_ids: strList(raw.server_ids) };
    case "fleet": {
      const summary = normalizeFleetSummary(raw.summary);
      return summary === null ? null : { type: "fleet", summary };
    }
    case "server": {
      const serverId = str(raw.server_id);
      const snapshot = normalizeServerSnapshot(raw.snapshot);
      if (serverId === null || snapshot === null) return null;
      return { type: "server", server_id: serverId, snapshot };
    }
    case "status": {
      const serverId = str(raw.server_id);
      if (serverId === null) return null;
      return { type: "status", server_id: serverId, status: serverStatus(raw.status) };
    }
    default:
      return null;
  }
}

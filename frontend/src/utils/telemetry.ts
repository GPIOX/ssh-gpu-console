/** Telemetry-derived helpers shared by lanes, fleet rows, and detail views. */

import type { GpuInfo, GpuProcessInfo, ServerRecord } from "../types/models";

/** Owner user names for one GPU, correlated by UUID (index fallback). */
export function gpuOwners(gpu: GpuInfo, processes: GpuProcessInfo[]): string[] {
  const users = new Set<string>();
  for (const process of processes) {
    const matchesUuid = gpu.uuid !== null && process.gpu_uuid === gpu.uuid;
    const matchesIndex = gpu.uuid === null && process.gpu_index === gpu.index;
    if (matchesUuid || matchesIndex) {
      if (process.user !== null) users.add(process.user);
    }
  }
  return [...users];
}

/** "alice, dana" or an em dash when nobody holds the GPU (prefix via t.gpu.users). */
export function formatOwners(owners: string[]): string {
  if (owners.length === 0) return "—";
  return owners.join(", ");
}

/** "GPU0" style lane identity. */
export function gpuLabel(index: number): string {
  return `GPU${index}`;
}

/** user@host:port identity for a registry record (port omitted when unset). */
export function serverEndpoint(record: ServerRecord): string {
  const user = record.username ?? "root";
  const host = record.port === null ? record.ssh_host : `${record.ssh_host}:${record.port}`;
  return `${user}@${host}`;
}

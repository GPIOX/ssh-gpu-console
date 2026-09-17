/**
 * Fleet sorting: pure functions over FleetEntry. Severity ranking puts
 * unreachable/broken servers first (offline < timeout < auth < host-key <
 * reconnecting < connecting < unknown < degraded < online); ties always break
 * by display name so the order is deterministic.
 */

import type { FleetSort } from "../../store/consoleStore";
import type { FleetEntry, ServerStatus } from "../../types/models";

const SEVERITY: Record<ServerStatus, number> = {
  offline: 0,
  timeout: 1,
  authentication_failed: 2,
  host_key_error: 3,
  reconnecting: 4,
  connecting: 5,
  unknown: 6,
  degraded: 7,
  online: 8,
};

/** Lower = more severe (sorts first in ascending status order). */
export function statusSeverity(status: ServerStatus): number {
  return SEVERITY[status];
}

function nameCompare(a: FleetEntry, b: FleetEntry): number {
  return a.display_name.localeCompare(b.display_name);
}

export function sortFleetEntries(entries: FleetEntry[], sort: FleetSort): FleetEntry[] {
  const sign = sort.direction === "desc" ? -1 : 1;
  return entries.slice().sort((a, b) => {
    let delta: number;
    switch (sort.key) {
      case "name":
        delta = nameCompare(a, b);
        break;
      case "status":
        delta = statusSeverity(a.status) - statusSeverity(b.status);
        break;
      case "gpus":
        delta = a.gpu_count - b.gpu_count;
        break;
    }
    return delta !== 0 ? delta * sign : nameCompare(a, b);
  });
}

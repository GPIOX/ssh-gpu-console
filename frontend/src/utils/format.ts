/**
 * Display formatters. `null`/non-finite input always renders "N/A" — the
 * contract forbids fabricating values the remote host did not report.
 * Byte-based values use 1024-based units: nvidia-smi reports MiB (binary), so
 * a "48GB" card must render "48.0 GB", not a decimal-SI 51.5 GB.
 */

const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"] as const;
const BYTE_BASE = 1024;

function unitIndexFor(bytes: number): number {
  let index = 0;
  let value = bytes;
  while (Math.abs(value) >= BYTE_BASE && index < BYTE_UNITS.length - 1) {
    value /= BYTE_BASE;
    index += 1;
  }
  return index;
}

function formatInUnit(bytes: number, unitIndex: number): string {
  const scaled = bytes / BYTE_BASE ** unitIndex;
  if (unitIndex === 0) return `${Math.round(scaled)}`;
  return scaled.toFixed(1);
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "N/A";
  const unitIndex = unitIndexFor(bytes);
  return `${formatInUnit(bytes, unitIndex)} ${BYTE_UNITS[unitIndex]}`;
}

/** Same-unit pair label, e.g. "18.2/24.0 GB" (unit picked from the total). */
export function formatBytesPair(used: number | null, total: number | null): string {
  if (total === null || !Number.isFinite(total)) return "N/A";
  if (used === null || !Number.isFinite(used)) {
    return `N/A/${formatInUnit(total, unitIndexFor(total))} ${BYTE_UNITS[unitIndexFor(total)]}`;
  }
  const unitIndex = unitIndexFor(total);
  return `${formatInUnit(used, unitIndex)}/${formatInUnit(total, unitIndex)} ${BYTE_UNITS[unitIndex]}`;
}

/** Network rates are bytes/second (counters come from /proc/net/dev deltas). */
export function formatBps(bps: number | null | undefined): string {
  if (bps === null || bps === undefined || !Number.isFinite(bps)) return "N/A";
  return `${formatBytes(bps)}/s`;
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "N/A";
  return `${value.toFixed(digits)}%`;
}

export function formatTemperature(celsius: number | null | undefined): string {
  if (celsius === null || celsius === undefined || !Number.isFinite(celsius)) return "N/A";
  return `${Math.round(celsius)}°`;
}

export function formatPower(
  watts: number | null | undefined,
  limitWatts: number | null | undefined,
): string {
  if (watts === null || watts === undefined || !Number.isFinite(watts)) return "N/A";
  if (limitWatts === null || limitWatts === undefined || !Number.isFinite(limitWatts)) {
    return `${Math.round(watts)}W`;
  }
  return `${Math.round(watts)}/${Math.round(limitWatts)}W`;
}

export function formatRelative(
  timestamp: string | number | null | undefined,
  now: number = Date.now(),
): string {
  if (timestamp === null || timestamp === undefined) return "—";
  const time = typeof timestamp === "number" ? timestamp : Date.parse(timestamp);
  if (!Number.isFinite(time)) return "—";
  const seconds = Math.max(0, Math.round((now - time) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function formatUptime(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) {
    return "N/A";
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "N/A";
  return `${Math.round(ms)} ms`;
}

/** Human wording for the status taxonomy, e.g. "authentication failed". */
export function formatStatusWord(status: string): string {
  return status.replaceAll("_", " ");
}

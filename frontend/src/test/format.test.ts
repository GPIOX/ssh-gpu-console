import { describe, expect, it } from "vitest";
import {
  formatBps,
  formatBytes,
  formatBytesPair,
  formatLatency,
  formatPercent,
  formatPower,
  formatRelative,
  formatStatusWord,
  formatTemperature,
  formatUptime,
} from "../utils/format";

describe("formatBytes", () => {
  it("renders 1024-based units (nvidia-smi semantics)", () => {
    expect(formatBytes(19541268941)).toBe("18.2 GB");
    expect(formatBytes(25769803776)).toBe("24.0 GB");
    expect(formatBytes(566_231_040)).toBe("540.0 MB"); // 540 * 1024^2
    expect(formatBytes(999)).toBe("999 B");
    expect(formatBytes(1_024)).toBe("1.0 KB");
    expect(formatBytes(0)).toBe("0 B");
  });

  it("renders N/A for missing values", () => {
    expect(formatBytes(null)).toBe("N/A");
    expect(formatBytes(undefined)).toBe("N/A");
    expect(formatBytes(Number.NaN)).toBe("N/A");
  });
});

describe("formatBytesPair", () => {
  it("uses one unit picked from the total", () => {
    expect(formatBytesPair(19541268941, 25769803776)).toBe("18.2/24.0 GB");
    expect(formatBytesPair(629145600, 25769803776)).toBe("0.6/24.0 GB");
  });

  it("handles missing halves", () => {
    expect(formatBytesPair(null, 25769803776)).toBe("N/A/24.0 GB");
    expect(formatBytesPair(1_000, null)).toBe("N/A");
  });
});

describe("formatBps", () => {
  it("renders byte rates", () => {
    expect(formatBps(1_234_000)).toBe("1.2 MB/s");
    expect(formatBps(348160)).toBe("340.0 KB/s");
    expect(formatBps(null)).toBe("N/A");
  });
});

describe("formatRelative", () => {
  const now = 1_700_000_000_000;

  it("renders seconds", () => {
    expect(formatRelative(now - 3_000, now)).toBe("3s ago");
    expect(formatRelative(now - 42_000, now)).toBe("42s ago");
  });

  it("renders minutes, hours, days", () => {
    expect(formatRelative(now - 90_000, now)).toBe("1m ago");
    expect(formatRelative(now - 5 * 60_000, now)).toBe("5m ago");
    expect(formatRelative(now - 2 * 3_600_000, now)).toBe("2h ago");
    expect(formatRelative(now - 3 * 86_400_000, now)).toBe("3d ago");
  });

  it("handles missing and invalid timestamps", () => {
    expect(formatRelative(null, now)).toBe("—");
    expect(formatRelative("not-a-date", now)).toBe("—");
    expect(formatRelative(now + 60_000, now)).toBe("0s ago");
  });
});

describe("telemetry value formatters", () => {
  it("temperature", () => {
    expect(formatTemperature(64.4)).toBe("64°");
    expect(formatTemperature(null)).toBe("N/A");
  });

  it("power with optional limit", () => {
    expect(formatPower(210, 350)).toBe("210/350W");
    expect(formatPower(210.6, null)).toBe("211W");
    expect(formatPower(null, 350)).toBe("N/A");
  });

  it("percent", () => {
    expect(formatPercent(72.4)).toBe("72%");
    expect(formatPercent(72.45, 1)).toBe("72.5%");
    expect(formatPercent(null)).toBe("N/A");
  });

  it("uptime and latency", () => {
    expect(formatUptime(1_036_800)).toBe("12d 0h");
    expect(formatUptime(4_000)).toBe("1h 6m");
    expect(formatUptime(120)).toBe("2m");
    expect(formatLatency(12.4)).toBe("12 ms");
    expect(formatLatency(null)).toBe("N/A");
  });

  it("status word", () => {
    expect(formatStatusWord("authentication_failed")).toBe("authentication failed");
    expect(formatStatusWord("online")).toBe("online");
  });
});

import type { GpuAvailability } from "../types/models";
import type { DotStatus } from "../design/StatusDot";

export type AvailabilityKey = "free" | "active" | "saturated" | "unavailable";

export interface AvailabilityView {
  /** i18n key under `t.gpu.*` — labels live in src/i18n, not here. */
  labelKey: AvailabilityKey;
  dot: DotStatus;
  tone: "free" | "active" | "saturated" | "na";
}

/**
 * Availability vocabulary (DESIGN.md). Green = available; cold = working;
 * amber outline = pressure; gray = not reported.
 */
export function availabilityView(availability: GpuAvailability): AvailabilityView {
  switch (availability) {
    case "free":
      return { labelKey: "free", dot: "online", tone: "free" };
    case "active":
      return { labelKey: "active", dot: "active", tone: "active" };
    case "saturated":
      return { labelKey: "saturated", dot: "stale", tone: "saturated" };
    case "unavailable":
      return { labelKey: "unavailable", dot: "unknown", tone: "na" };
  }
}

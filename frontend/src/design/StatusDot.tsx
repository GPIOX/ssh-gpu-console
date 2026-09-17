import { cx } from "../utils/cx";
import type { ServerStatus } from "../types/models";
import "./status-dot.css";

/**
 * Visual dot states. `active`/`unknown` extend the DESIGN.md set so GPU
 * availability (ACTIVE) and never-seen servers render without overloading
 * "connecting" (which pulses).
 */
export type DotStatus =
  | "online"
  | "active"
  | "stale"
  | "offline"
  | "connecting"
  | "error"
  | "unknown";

/** Map the ServerStatus taxonomy onto dot visuals. */
export function dotStatusFromServerStatus(status: ServerStatus): DotStatus {
  switch (status) {
    case "online":
      return "online";
    case "degraded":
      return "stale";
    case "offline":
    case "timeout":
      return "offline";
    case "connecting":
    case "reconnecting":
      return "connecting";
    case "authentication_failed":
    case "host_key_error":
      return "error";
    case "unknown":
      return "unknown";
  }
}

export interface StatusDotProps {
  status: DotStatus;
  /** Optional 2px halo ring around the dot. */
  ring?: boolean;
  className?: string;
  label?: string;
}

/** 10px status dot. Pulses (1.2s) only while connecting/reconnecting. */
export function StatusDot({ status, ring, className, label }: StatusDotProps) {
  return (
    <span
      className={cx(
        "status-dot",
        `status-dot--${status}`,
        ring && "status-dot--ring",
        status === "connecting" && "status-dot--pulse",
        className,
      )}
      role="img"
      aria-label={label ?? status}
    />
  );
}

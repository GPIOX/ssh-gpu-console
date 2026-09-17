import { cx } from "../utils/cx";
import "./segmented-meter.css";

export type MeterState = "ok" | "warn" | "crit";

/** Auto state thresholds: pressure goes amber, saturation goes red. */
export const METER_WARN_THRESHOLD = 75;
export const METER_CRIT_THRESHOLD = 90;

export function meterStateForValue(
  value: number,
  warn = METER_WARN_THRESHOLD,
  crit = METER_CRIT_THRESHOLD,
): MeterState {
  if (value >= crit) return "crit";
  if (value >= warn) return "warn";
  return "ok";
}

export interface SegmentedMeterProps {
  /** Reported value; null renders an empty track + "N/A". */
  value: number | null;
  /** Upper bound of the value range. */
  max?: number;
  /** Segment count; each segment is 3px wide with a 1px gap. */
  segments?: number;
  /** Fill color intent. "auto" derives warn/crit from the value. */
  state?: MeterState | "auto";
  /** Show the mono value label adjacent to the strip (default true). */
  showValue?: boolean;
  /** Override the label text, e.g. "18.2/24.0 GB". */
  valueText?: string;
  className?: string;
  "aria-label"?: string;
}

const DEFAULT_SEGMENTS = 24;

/**
 * The signature primitive: a segmented LED strip over a --surface-3 track.
 * Filled segments use state color at 90% opacity; the value sits adjacent in mono.
 */
export function SegmentedMeter({
  value,
  max = 100,
  segments = DEFAULT_SEGMENTS,
  state = "auto",
  showValue = true,
  valueText,
  className,
  "aria-label": ariaLabel,
}: SegmentedMeterProps) {
  const bounded = value === null ? 0 : Math.max(0, Math.min(max, value));
  const filled = Math.round((bounded / (max || 1)) * segments);
  const resolvedState: MeterState = state === "auto" ? meterStateForValue(bounded) : state;
  const label = valueText ?? (value === null ? "N/A" : `${Math.round(value)}%`);
  const segmentElements = Array.from({ length: segments }, (_unused, index) =>
    index < filled ? (
      <span
        key={index}
        className={cx("meter__seg", "meter__seg--filled", `meter__seg--${resolvedState}`)}
      />
    ) : (
      <span key={index} className="meter__seg" />
    ),
  );
  return (
    <span className={cx("meter", className)} aria-label={ariaLabel}>
      <span
        className="meter__strip"
        role={value === null ? undefined : "meter"}
        aria-valuemin={value === null ? undefined : 0}
        aria-valuemax={value === null ? undefined : max}
        aria-valuenow={value === null ? undefined : Math.round(bounded)}
        aria-hidden={value === null ? true : undefined}
      >
        <span
          className={cx("meter__fill", `meter__fill--${resolvedState}`)}
          style={{ width: `${value === null ? 0 : (bounded / (max || 1)) * 100}%` }}
        />
        {segmentElements}
      </span>
      {showValue && (
        <span className={cx("meter__value", "tnum", value === null && "meter__value--empty")}>
          {label}
        </span>
      )}
    </span>
  );
}

import { SegmentedMeter, meterStateForValue, type MeterState } from "../design/SegmentedMeter";
import { formatBytesPair } from "../utils/format";
import { cx } from "../utils/cx";
import "./vram-gauge.css";

const VRAM_WARN = 80;
const VRAM_CRIT = 95;

export interface VramGaugeProps {
  usedB: number | null;
  totalB: number | null;
  /** Precomputed percent; derived from used/total when absent. */
  percent?: number | null;
  segments?: number;
  className?: string;
}

/** VRAM as the signature segmented track + mono "used/total GB" label. */
export function VramGauge({ usedB, totalB, percent, segments = 18, className }: VramGaugeProps) {
  const value =
    percent ??
    (usedB !== null && totalB !== null && totalB > 0 ? (usedB / totalB) * 100 : null);
  const label = totalB !== null ? formatBytesPair(usedB, totalB) : "N/A";
  const state: MeterState = value === null ? "ok" : meterStateForValue(value, VRAM_WARN, VRAM_CRIT);
  return (
    <SegmentedMeter
      className={cx("vram-gauge", className)}
      value={value}
      segments={segments}
      state={state}
      valueText={label}
      aria-label="VRAM"
    />
  );
}

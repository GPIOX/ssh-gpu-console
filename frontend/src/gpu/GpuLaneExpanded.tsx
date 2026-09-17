import type { ReactNode } from "react";
import { meterStateForValue } from "../design/SegmentedMeter";
import { useT } from "../i18n";
import { cx } from "../utils/cx";
import { formatPercent } from "../utils/format";
import { gpuLabel } from "../utils/telemetry";
import type { GpuInfo } from "../types/models";
import { GpuLane } from "./GpuLane";
import { Sparkline } from "./Sparkline";
import "./gpu-lane-expanded.css";

export interface GpuLaneExpandedProps {
  gpu: GpuInfo;
  /** Utilization samples (≤120) from the bounded store history. */
  utilizationHistory?: number[];
  /** VRAM percent samples (≤120). */
  vramHistory?: number[];
  owners?: string[];
  /** Process rows slot rendered under the lane. */
  children?: ReactNode;
  className?: string;
}

/**
 * Expanded GPU surface: identity row with hero utilization numeral + 120-pt
 * sparklines (util + VRAM), the compressed lane metrics, then a process rows slot.
 */
export function GpuLaneExpanded({
  gpu,
  utilizationHistory,
  vramHistory,
  owners,
  children,
  className,
}: GpuLaneExpandedProps) {
  const t = useT();
  return (
    <section className={cx("gpu-lane-x", className)}>
      <header className="gpu-lane-x__head">
        <span className="gpu-lane-x__name mono">{gpuLabel(gpu.index)}</span>
        <span className="gpu-lane-x__model" title={gpu.name ?? undefined}>
          {gpu.name ?? "unknown model"}
        </span>
        <span className="gpu-lane-x__numeral mono tnum">{formatPercent(gpu.utilization_percent, 1)}</span>
        <div
          className={cx(
            "gpu-lane-x__spark",
            "gpu-lane-x__spark--util",
            `gpu-lane-x__spark--${meterStateForValue(gpu.utilization_percent ?? 0)}`,
          )}
        >
          <Sparkline values={utilizationHistory ?? []} fill label={t.gpu.utilization} />
        </div>
        <span className="gpu-lane-x__vram-label mono tnum">
          {formatPercent(gpu.vram_percent, 1)}
        </span>
        <div className="gpu-lane-x__spark gpu-lane-x__spark--vram">
          <Sparkline values={vramHistory ?? []} fill label={t.gpu.vramHistory} />
        </div>
      </header>
      <GpuLane gpu={gpu} owners={owners} labelHidden className="gpu-lane-x__lane" />
      {children !== undefined && children !== null && (
        <div className="gpu-lane-x__procs">{children}</div>
      )}
    </section>
  );
}

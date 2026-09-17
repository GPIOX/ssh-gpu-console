import { SegmentedMeter } from "../design/SegmentedMeter";
import { StatusDot } from "../design/StatusDot";
import type { GpuInfo } from "../types/models";
import { cx } from "../utils/cx";
import { gpuLabel } from "../utils/telemetry";
import { tf, useT } from "../i18n";
import { availabilityView } from "./availability";
import { ThermalPower } from "./ThermalPower";
import { VramGauge } from "./VramGauge";
import "./gpu-lane.css";

export interface GpuLaneProps {
  gpu: GpuInfo;
  /** Owner user names correlated from gpu_processes (see gpuOwners). */
  owners?: string[];
  /** Hide the GPU-index label (used inside GpuLaneExpanded, which labels the card). */
  labelHidden?: boolean;
  className?: string;
}

/**
 * Compressed one-line GPU lane (~22px):
 * name → util meter → VRAM gauge → temp/power → availability → owners.
 */
export function GpuLane({ gpu, owners, labelHidden = false, className }: GpuLaneProps) {
  const t = useT();
  const availability = availabilityView(gpu.availability);
  return (
    <div className={cx("gpu-lane", labelHidden && "gpu-lane--nolabel", className)}>
      <span className="gpu-lane__name mono" aria-hidden={labelHidden || undefined}>
        {labelHidden ? null : gpuLabel(gpu.index)}
      </span>
      <SegmentedMeter
        className="gpu-lane__util"
        value={gpu.utilization_percent}
        aria-label={`GPU${gpu.index} utilization`}
      />
      <VramGauge
        className="gpu-lane__vram"
        usedB={gpu.vram_used_b}
        totalB={gpu.vram_total_b}
        percent={gpu.vram_percent}
      />
      <ThermalPower
        className="gpu-lane__thermal"
        temperatureC={gpu.temperature_c}
        powerWatts={gpu.power_watts}
        powerLimitWatts={gpu.power_limit_watts}
      />
      <span className={cx("gpu-lane__avail", `gpu-lane__avail--${availability.tone}`)}>
        <StatusDot status={availability.dot} label={t.gpu[availability.labelKey]} />
        {t.gpu[availability.labelKey]}
      </span>
      <span
        className="gpu-lane__owners mono"
        title={owners?.length ? tf(t.gpu.users, { names: owners.join(", ") }) : undefined}
      >
        {owners === undefined || owners.length === 0
          ? "—"
          : tf(t.gpu.users, { names: owners.join(", ") })}
      </span>
    </div>
  );
}

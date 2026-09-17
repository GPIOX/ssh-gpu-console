/**
 * GPUs tab — one expanded telemetry surface per GPU: hero utilization numeral,
 * sparklines fed from the bounded store history (rendered only once there are
 * ≥2 points — Sparkline hides shorter series), lane metrics, and the compute
 * processes held by that GPU.
 */

import { EmptyState, Panel } from "../../design";
import { GpuLaneExpanded } from "../../gpu";
import { useT } from "../../i18n";
import type { GpuHistorySeries } from "../../store/consoleStore";
import type {
  GpuInfo,
  GpuProcessInfo,
  ProcessInfo,
  ServerSnapshot,
} from "../../types/models";
import { formatBytes } from "../../utils/format";
import { gpuOwners } from "../../utils/telemetry";
import "./server-page.css";

export interface GpusTabProps {
  snapshot: ServerSnapshot;
  /** Store history lookup (may be missing entries right after connect). */
  resolveSeries: (gpuIndex: number) => GpuHistorySeries | undefined;
}

/** Match a compute process to its GPU by UUID first, falling back to index. */
function processBelongsTo(gpu: GpuInfo, proc: GpuProcessInfo): boolean {
  if (gpu.uuid !== null && proc.gpu_uuid !== null) {
    return gpu.uuid === proc.gpu_uuid;
  }
  return proc.gpu_index === gpu.index;
}

/**
 * The compute-apps query does not carry the owning user; resolve it from the
 * system process list by pid. "unknown" only when the process list (which may
 * have sampled slightly earlier) never saw the pid either.
 */
/** nvidia-smi reports compute-app names as full paths; show the binary. */
function shortName(name: string | null): string | null {
  if (name === null) return null;
  const last = name.split("/").pop();
  return last === "" ? name : (last ?? name);
}

function resolveUser(
  t: ReturnType<typeof useT>,
  proc: GpuProcessInfo,
  processes: ProcessInfo[],
): string {
  if (proc.user !== null) return proc.user;
  const match = processes.find((p) => p.pid === proc.pid);
  return match?.user ?? t.common.unknown;
}

export function GpusTab({ snapshot, resolveSeries }: GpusTabProps) {
  const t = useT();
  if (snapshot.gpus.length === 0) {
    const quiet = snapshot.errors.gpu === "unavailable";
    return (
      <Panel>
        <EmptyState
          title={quiet ? t.overview.noGpu : t.overview.noGpusReported}
          hint={
            quiet
              ? t.overview.noGpuHint
              : (snapshot.errors.gpu ?? t.overview.noGpusReportedHint)
          }
        />
      </Panel>
    );
  }

  return (
    <div className="srv-stack">
      {snapshot.gpus.map((gpu) => {
        const series = resolveSeries(gpu.index);
        const procs = snapshot.gpu_processes.filter((proc) => processBelongsTo(gpu, proc));
        return (
          <Panel key={gpu.index}>
            <GpuLaneExpanded
              gpu={gpu}
              utilizationHistory={series?.utilization}
              vramHistory={series?.vram}
              owners={gpuOwners(gpu, snapshot.gpu_processes)}
            >
              {procs.length === 0 ? (
                <p className="srv-muted">{t.gpu.noProcs}</p>
              ) : (
                <div className="srv-gpu-procs">
                  <div className="srv-gpu-proc srv-gpu-proc--head" aria-hidden="true">
                    <span>{t.gpus.colPid}</span>
                    <span>{t.gpus.colUser}</span>
                    <span className="srv-gpu-proc__name">{t.gpus.colProcess}</span>
                    <span>{t.gpus.colVram}</span>
                    <span>{t.gpus.colCommand}</span>
                  </div>
                  {procs.map((proc) => (
                    <div
                      key={`${proc.pid}-${proc.gpu_index ?? proc.gpu_uuid ?? ""}`}
                      className="srv-gpu-proc"
                    >
                      <span className="mono tnum">{proc.pid}</span>
                      <span>{resolveUser(t, proc, snapshot.processes)}</span>
                      <span className="srv-gpu-proc__name" title={proc.process_name ?? undefined}>
                        {shortName(proc.process_name) ?? "—"}
                      </span>
                      <span className="mono tnum">{formatBytes(proc.used_memory_b)}</span>
                      <span className="srv-gpu-proc__cmd mono" title={proc.command ?? undefined}>
                        {proc.command ?? "—"}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </GpuLaneExpanded>
          </Panel>
        );
      })}
    </div>
  );
}

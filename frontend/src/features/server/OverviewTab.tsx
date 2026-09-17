/**
 * Overview tab — the GPU lanes are the hero, followed by the CPU/RAM pressure
 * band, a storage warning (only when a mount crosses the warn threshold),
 * network RX/TX, and the top five processes by CPU. Broad sections, not a
 * grid of stat cards (DESIGN.md "Server detail composition").
 */

import { Chip, EmptyState, Panel, Section, SegmentedMeter, meterStateForValue } from "../../design";
import { GpuLane } from "../../gpu";
import { tf, useT } from "../../i18n";
import type { CpuInfo, ServerSnapshot } from "../../types/models";
import { formatBytes, formatBytesPair, formatBps, formatPercent } from "../../utils/format";
import { gpuOwners } from "../../utils/telemetry";
import "./server-page.css";

const STORAGE_WARN = 80;
const STORAGE_CRIT = 90;
const TOP_PROCESS_COUNT = 5;

/** Sum of the reported rates; null when the host reported none (no two samples yet). */
function sumRates(values: Array<number | null>): number | null {
  let total: number | null = null;
  for (const value of values) {
    if (value !== null && Number.isFinite(value)) {
      total = (total ?? 0) + value;
    }
  }
  return total;
}

function rateText(bps: number | null): string {
  return bps === null ? "—" : formatBps(bps);
}

function loadValuesText(t: ReturnType<typeof useT>, cpu: CpuInfo | null): string {
  if (cpu === null) return t.common.na;
  const one = cpu.load_1 === null ? t.common.na : cpu.load_1.toFixed(2);
  const five = cpu.load_5 === null ? t.common.na : cpu.load_5.toFixed(2);
  const fifteen = cpu.load_15 === null ? t.common.na : cpu.load_15.toFixed(2);
  return `${one} ${five} ${fifteen}`;
}

function coresText(t: ReturnType<typeof useT>, cpu: CpuInfo | null, snapshot: ServerSnapshot): string {
  const logical = cpu?.cores_logical ?? snapshot.system?.cores_logical;
  return logical === null || logical === undefined
    ? t.overview.coresNa
    : tf(t.overview.cores, { n: logical });
}

function GpuSection({ snapshot }: { snapshot: ServerSnapshot }) {
  const t = useT();
  const gpus = snapshot.gpus;
  if (gpus.length === 0) {
    const quiet = snapshot.errors.gpu === "unavailable";
    return (
      <EmptyState
        title={quiet ? t.overview.noGpu : t.overview.noGpusReported}
        hint={
          quiet
            ? t.overview.noGpuHint
            : (snapshot.errors.gpu ?? t.overview.noGpusReportedHint)
        }
      />
    );
  }
  return (
    <div className="srv-lanes">
      {gpus.map((gpu) => (
        <GpuLane key={gpu.index} gpu={gpu} owners={gpuOwners(gpu, snapshot.gpu_processes)} />
      ))}
    </div>
  );
}

export function OverviewTab({ snapshot }: { snapshot: ServerSnapshot }) {
  const t = useT();
  const cpu = snapshot.cpu;
  const memory = snapshot.memory;

  // Highest-percent mount; the overview carries a warning only under pressure.
  const worstMount = [...snapshot.storage].sort(
    (a, b) => (b.percent ?? -1) - (a.percent ?? -1),
  )[0];
  const storageWarning =
    worstMount !== undefined && (worstMount.percent ?? 0) >= STORAGE_WARN ? worstMount : null;

  const rxTotal = sumRates(snapshot.network.map((iface) => iface.rx_bps));
  const txTotal = sumRates(snapshot.network.map((iface) => iface.tx_bps));

  const topProcesses = [...snapshot.processes]
    .sort((a, b) => (b.cpu_percent ?? -1) - (a.cpu_percent ?? -1))
    .slice(0, TOP_PROCESS_COUNT);

  return (
    <div className="srv-stack">
      <Panel>
        <Section
          title={t.tabs.gpus}
          meta={
            snapshot.gpus.length > 0
              ? tf(
                  snapshot.gpus.length === 1 ? t.overview.deviceOne : t.overview.deviceMany,
                  { n: snapshot.gpus.length },
                )
              : undefined
          }
        >
          <GpuSection snapshot={snapshot} />
        </Section>
      </Panel>

      <Panel>
        <Section title={t.overview.cpuRamPressure}>
          <div className="srv-pressure">
            <div className="srv-pressure__cell">
              <span className="micro-label">{t.overview.cpu}</span>
              <span className="srv-pressure__numeral mono tnum">
                {formatPercent(cpu?.percent ?? null, 0)}
              </span>
              <SegmentedMeter
                value={cpu?.percent ?? null}
                aria-label={t.overview.cpuUtilization}
              />
              <span className="srv-pressure__sub mono tnum">
                {tf(t.overview.load, {
                  loads: loadValuesText(t, cpu),
                  cores: coresText(t, cpu, snapshot),
                })}
              </span>
            </div>
            <div className="srv-pressure__cell">
              <span className="micro-label">{t.overview.ram}</span>
              <span className="srv-pressure__numeral mono tnum">
                {formatPercent(memory?.percent ?? null, 0)}
              </span>
              <SegmentedMeter
                value={memory?.percent ?? null}
                aria-label={t.overview.ramUtilization}
              />
              <span className="srv-pressure__sub mono tnum">
                {formatBytesPair(memory?.used_b ?? null, memory?.total_b ?? null)}
              </span>
            </div>
          </div>
        </Section>
      </Panel>

      {storageWarning !== null && (
        <Panel>
          <Section
            title={t.overview.storagePressure}
            meta={
              <Chip tone="warn" mono>
                {tf(t.overview.percentUsed, { n: Math.round(storageWarning.percent ?? 0) })}
              </Chip>
            }
          >
            <div className="srv-diskwarn">
              <span className="srv-diskwarn__device mono">{storageWarning.device}</span>
              <span className="srv-diskwarn__mount mono">{storageWarning.mount}</span>
              <SegmentedMeter
                className="srv-diskwarn__meter"
                value={storageWarning.percent}
                state={
                  storageWarning.percent === null
                    ? "ok"
                    : meterStateForValue(storageWarning.percent, STORAGE_WARN, STORAGE_CRIT)
                }
                valueText={formatBytesPair(storageWarning.used_b, storageWarning.total_b)}
                aria-label={tf(t.storage.usageOf, { mount: storageWarning.mount })}
              />
              <span className="srv-diskwarn__free mono tnum">
                {tf(t.storage.free, { size: formatBytes(storageWarning.free_b) })}
              </span>
            </div>
          </Section>
        </Panel>
      )}

      <Panel>
        <Section
          title={t.overview.network}
          meta={tf(t.overview.rxTx, { rx: rateText(rxTotal), tx: rateText(txTotal) })}
        >
          {snapshot.network.length === 0 ? (
            <EmptyState title={t.network.empty} hint={t.network.emptyHint} />
          ) : (
            <div className="srv-netlist">
              {snapshot.network.map((iface) => (
                <div key={iface.name} className="srv-netlist__row">
                  <span className="srv-netlist__name mono">{iface.name}</span>
                  <span className="srv-netlist__ip mono">{iface.ip ?? "—"}</span>
                  <span className="mono tnum">{tf(t.overview.rx, { rate: rateText(iface.rx_bps) })}</span>
                  <span className="mono tnum">{tf(t.overview.tx, { rate: rateText(iface.tx_bps) })}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </Panel>

      <Panel>
        <Section
          title={t.overview.topProcesses}
          meta={
            snapshot.processes.length > 0
              ? tf(t.overview.topProcessesMeta, { n: snapshot.processes.length })
              : undefined
          }
        >
          {topProcesses.length === 0 ? (
            <EmptyState title={t.processes.empty} hint={t.processes.emptyHint} />
          ) : (
            <div className="srv-topprocs">
              {topProcesses.map((proc) => (
                <div key={proc.pid} className="srv-topprocs__row">
                  <span className="mono tnum">{proc.pid}</span>
                  <span className="srv-topprocs__user">{proc.user ?? "—"}</span>
                  <span className="srv-topprocs__name">{proc.name ?? "—"}</span>
                  <span className="mono tnum">{formatPercent(proc.cpu_percent, 1)}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </Panel>
    </div>
  );
}

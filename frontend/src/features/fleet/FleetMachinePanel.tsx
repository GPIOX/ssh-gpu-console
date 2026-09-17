/**
 * One machine panel — a server as a working machine, not a table row
 * (DESIGN.md "Machine panel"). Identity zone, CPU/RAM pressure band, GPU zone
 * (real GpuLanes when a snapshot exists, otherwise an aggregate summary line),
 * and a `⋯` menu holding every secondary action. Row actions never render as
 * permanent buttons.
 */

import { useEffect, useState } from "react";
import { ArrowSquareOut, Eye, Plugs, Prohibit, Trash } from "@phosphor-icons/react";
import {
  Button,
  Chip,
  Dialog,
  Menu,
  Panel,
  SegmentedMeter,
  StatusDot,
  dotStatusFromServerStatus,
  type ChipTone,
  type MenuItem,
} from "../../design";
import { GpuLane, VramGauge } from "../../gpu";
import { api } from "../../services/api";
import { useConsoleStore } from "../../store/consoleStore";
import type { FleetEntry, FleetGpu, GpuInfo, ServerSnapshot } from "../../types/models";
import { cx } from "../../utils/cx";
import { gpuOwners } from "../../utils/telemetry";
import { tf, useRelative, useT } from "../../i18n";

const MAX_LANES = 4;
const NOTICE_MS = 5000;

/** Statuses that mean "no live telemetry" — meters recede to text-4 at 55%. */
const UNREACHABLE = new Set(["offline", "timeout", "authentication_failed", "host_key_error"]);

interface Notice {
  tone: ChipTone;
  text: string;
  title?: string;
}

export interface FleetMachinePanelProps {
  entry: FleetEntry;
  snapshot: ServerSnapshot | undefined;
  /** Shared 1Hz clock sample for relative freshness labels. */
  now: number;
}

/** Widest reported mount percent from a snapshot, for the disk warning chip. */
function maxDiskPercent(snapshot: ServerSnapshot | undefined): number | null {
  if (snapshot === undefined) return null;
  let max: number | null = null;
  for (const mount of snapshot.storage) {
    if (mount.percent !== null && (max === null || mount.percent > max)) max = mount.percent;
  }
  return max;
}

/**
 * Lift a fleet-tier FleetGpu into the compressed lane shape. Unreported
 * fields stay null (VRAM percent is derived by VramGauge from used/total).
 */
function fleetGpuToLane(gpu: FleetGpu): GpuInfo {
  return {
    index: gpu.index,
    uuid: null,
    name: null,
    utilization_percent: gpu.utilization_percent,
    vram_used_b: gpu.vram_used_b,
    vram_total_b: gpu.vram_total_b,
    vram_percent: null,
    temperature_c: gpu.temperature_c,
    power_watts: gpu.power_watts ?? null,
    power_limit_watts: gpu.power_limit_watts ?? null,
    fan_percent: null,
    availability: gpu.availability,
    process_count: 0,
  };
}

export function FleetMachinePanel({ entry, snapshot, now }: FleetMachinePanelProps) {
  const navigate = useConsoleStore((state) => state.navigate);
  const t = useT();
  const relative = useRelative();
  const [notice, setNotice] = useState<Notice | null>(null);
  const [testing, setTesting] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);

  useEffect(() => {
    if (notice === null) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  const openServer = () => {
    navigate(`#/server/${encodeURIComponent(entry.server_id)}/overview`);
  };

  const runTest = async () => {
    setTesting(true);
    setNotice(null);
    try {
      const result = await api.testConnection(entry.server_id);
      setNotice(
        result.ok
          ? {
              tone: "ok",
              text:
                result.latency_ms !== null
                  ? tf(t.fleet.testOk, { ms: Math.round(result.latency_ms) })
                  : t.status.online,
              title: result.detail,
            }
          : { tone: "crit", text: t.status[result.status], title: result.detail },
      );
    } catch (cause) {
      setNotice({
        tone: "crit",
        text: t.fleet.testFailed,
        title: cause instanceof Error ? cause.message : "request failed",
      });
    } finally {
      setTesting(false);
    }
  };

  const toggleEnabled = async () => {
    setNotice(null);
    try {
      await api.patchServer(entry.server_id, { enabled: !entry.enabled });
      const store = useConsoleStore.getState();
      await Promise.all([store.loadServers(), store.loadFleet()]);
    } catch (cause) {
      setNotice({
        tone: "crit",
        text: entry.enabled ? t.fleet.disableFailed : t.fleet.enableFailed,
        title: cause instanceof Error ? cause.message : "request failed",
      });
    }
  };

  const removeServer = async () => {
    setNotice(null);
    try {
      await api.deleteServer(entry.server_id);
      setConfirmRemove(false);
      const store = useConsoleStore.getState();
      await Promise.all([store.loadServers(), store.loadFleet()]);
    } catch (cause) {
      setConfirmRemove(false);
      setNotice({
        tone: "crit",
        text: t.fleet.removeFailed,
        title: cause instanceof Error ? cause.message : "request failed",
      });
    }
  };

  const menuItems: MenuItem[] = [
    {
      id: "test",
      label: testing ? t.fleet.testing : t.fleet.testConnection,
      icon: <Plugs size={14} />,
      disabled: testing,
      onSelect: () => void runTest(),
    },
    { id: "open", label: t.common.open, icon: <ArrowSquareOut size={14} />, onSelect: openServer },
    entry.enabled
      ? {
          id: "toggle",
          label: t.common.disable,
          icon: <Prohibit size={14} />,
          onSelect: () => void toggleEnabled(),
        }
      : {
          id: "toggle",
          label: t.common.enable,
          icon: <Eye size={14} />,
          onSelect: () => void toggleEnabled(),
        },
    {
      id: "remove",
      label: t.common.remove,
      icon: <Trash size={14} />,
      danger: true,
      onSelect: () => setConfirmRemove(true),
    },
  ];

  const dot = dotStatusFromServerStatus(entry.status);
  const ring = dot === "offline" || dot === "error";
  const stale = snapshot?.stale === true;
  const dim = stale || UNREACHABLE.has(entry.status);
  const noticeChip = notice !== null && (
    <Chip tone={notice.tone} mono title={notice.title} className="fleet-panel__notice">
      {notice.text}
    </Chip>
  );
  const menu = (
    <div className="fleet-panel__menu" onClick={(event) => event.stopPropagation()}>
      <Menu
        items={menuItems}
        triggerLabel={tf(t.fleet.serverActions, { name: entry.display_name })}
      />
    </div>
  );

  // Disabled servers collapse to a single quiet identity line.
  if (!entry.enabled) {
    return (
      <Panel
        interactive
        onClick={openServer}
        className="fleet-panel fleet-panel--disabled"
      >
        <div className="fleet-panel__head">
          <StatusDot status={dot} ring={ring} label={entry.status} />
          <div className="fleet-panel__id fleet-panel__id--row">
            <span className="fleet-panel__name">{entry.display_name}</span>
            <span className="fleet-panel__endpoint mono">{entry.ssh_endpoint ?? "—"}</span>
            <Chip tone="neutral">{t.settings.disabled}</Chip>
          </div>
          <span
            className="fleet-panel__fresh mono tnum"
            title={entry.updated_at ?? undefined}
          >
            {tf(t.common.updated, { ago: relative(entry.updated_at, now) })}
          </span>
          {noticeChip}
          {menu}
        </div>
      </Panel>
    );
  }

  const platformParts: string[] = [];
  if (entry.gpu_model !== null) platformParts.push(`${entry.gpu_model} ×${entry.gpu_count}`);
  if (entry.os_pretty !== null) platformParts.push(entry.os_pretty);

  // GPU zone priority: full snapshot lanes > fleet-tier entry.gpus lanes >
  // aggregate summary line > quiet "no GPUs reported".
  const laneSnapshot = snapshot !== undefined && snapshot.gpus.length > 0 ? snapshot : null;
  const fleetGpus = laneSnapshot === null ? (entry.gpus ?? []) : [];
  const visibleLanes =
    laneSnapshot !== null
      ? laneSnapshot.gpus.slice(0, MAX_LANES).map((gpu) => ({
          gpu,
          owners: gpuOwners(gpu, laneSnapshot.gpu_processes),
        }))
      : fleetGpus.slice(0, MAX_LANES).map((gpu) => ({
          gpu: fleetGpuToLane(gpu),
          owners: gpu.users ?? undefined,
        }));
  const laneTotal =
    laneSnapshot !== null
      ? Math.max(entry.gpu_count, laneSnapshot.gpus.length)
      : Math.max(entry.gpu_count, fleetGpus.length);
  const hiddenLanes = Math.max(0, laneTotal - visibleLanes.length);
  const diskPercent = maxDiskPercent(snapshot);

  const staleAgo = relative(snapshot?.generated_at ?? entry.updated_at, now);
  const staleLabel = staleAgo === "—" ? t.status.stale : tf(t.fleet.stale, { ago: staleAgo });

  return (
    <Panel
      interactive
      onClick={openServer}
      className={cx("fleet-panel", dim && "fleet-panel--dim", stale && "fleet-panel--stale")}
    >
      <div className="fleet-panel__head">
        <StatusDot status={dot} ring={ring} label={entry.status} />
        <div className="fleet-panel__id">
          <div className="fleet-panel__name-row">
            <span className="fleet-panel__name">{entry.display_name}</span>
            {stale && (
              <Chip tone="warn" mono>
                {staleLabel}
              </Chip>
            )}
          </div>
          <span className="fleet-panel__endpoint mono">{entry.ssh_endpoint ?? "—"}</span>
        </div>
        <div className="fleet-panel__meta">
          {platformParts.length > 0 && (
            <span className="fleet-panel__platform">{platformParts.join(" · ")}</span>
          )}
          <span className="fleet-panel__fresh mono tnum" title={entry.updated_at ?? undefined}>
            {tf(t.common.updated, { ago: relative(entry.updated_at, now) })}
          </span>
        </div>
        {noticeChip}
        {menu}
      </div>

      <div className="fleet-panel__pressure">
        <span className="fleet-pressure__item">
          <span className="micro-label">{t.overview.cpu}</span>
          <SegmentedMeter
            value={entry.cpu_percent}
            segments={16}
            aria-label={tf(t.fleet.cpuAria, { name: entry.display_name })}
          />
        </span>
        <span className="fleet-pressure__item">
          <span className="micro-label">{t.overview.ram}</span>
          <SegmentedMeter
            value={entry.memory_percent}
            segments={16}
            aria-label={tf(t.fleet.ramAria, { name: entry.display_name })}
          />
        </span>
        {entry.disk_warning && (
          <Chip tone="warn" mono title={t.fleet.diskWarnTitle}>
            {diskPercent !== null
              ? tf(t.fleet.diskWarn, { percent: `${Math.round(diskPercent)}%` })
              : t.fleet.diskWarnBare}
          </Chip>
        )}
      </div>

      <div className="fleet-panel__gpus">
        {laneSnapshot !== null || fleetGpus.length > 0 ? (
          <>
            {visibleLanes.map(({ gpu, owners }) => (
              <GpuLane key={gpu.index} gpu={gpu} owners={owners} />
            ))}
            {hiddenLanes > 0 && (
              <span className="fleet-panel__more mono tnum">
                {tf(t.fleet.moreGpus, { n: hiddenLanes })}
              </span>
            )}
          </>
        ) : entry.gpu_count > 0 ? (
          <div className="fleet-summary">
            <span className="micro-label">{t.tabs.gpus}</span>
            <span className="fleet-summary__counts mono tnum">
              {tf(t.fleet.gpuSummary, {
                count: entry.gpu_count,
                busy: entry.gpu_busy,
                free: entry.gpu_free,
              })}
            </span>
            {entry.vram_total_b !== null && (
              <VramGauge
                usedB={entry.vram_used_b}
                totalB={entry.vram_total_b}
                segments={12}
              />
            )}
          </div>
        ) : (
          <span className="fleet-panel__nogpu mono">{t.fleet.noGpus}</span>
        )}
      </div>

      <Dialog
        open={confirmRemove}
        onClose={() => setConfirmRemove(false)}
        title={tf(t.fleet.removeConfirm, { name: entry.display_name })}
      >
        <p className="fleet-dialog__text">
          {tf(t.fleet.removeTitle, { name: entry.display_name })}{" "}
          (<span className="mono">{entry.ssh_endpoint ?? entry.server_id}</span>)
        </p>
        <p className="fleet-dialog__text">
          {tf(t.fleet.removeBody, { name: entry.display_name })}
        </p>
        <div className="fleet-dialog__actions">
          <Button onClick={() => setConfirmRemove(false)}>{t.common.cancel}</Button>
          <Button variant="primary" onClick={() => void removeServer()}>
            {tf(t.fleet.removeConfirm, { name: entry.display_name })}
          </Button>
        </div>
      </Dialog>
    </Panel>
  );
}

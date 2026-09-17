import { useNow } from "../utils/clock";
import {
  Chip,
  EmptyState,
  ErrorPanel,
  Panel,
  Section,
  SegmentedMeter,
  Skeleton,
  Spinner,
  StatusDot,
  dotStatusFromServerStatus,
} from "../design";
import { GpuLane, GpuLaneExpanded } from "../gpu";
import { tf, useT } from "../i18n";
import { formatRelative } from "../utils/format";
import { gpuOwners } from "../utils/telemetry";
import {
  fixtureGpuHistory,
  fixtureServers,
  fixtureSnapshots,
} from "../data/fixtures";
import "./preview.css";

const MAX_LANES = 4;

function PreviewServer({ serverId }: { serverId: string }) {
  const t = useT();
  const now = useNow();
  const record = fixtureServers.find((server) => server.server_id === serverId);
  const snapshot = fixtureSnapshots[serverId];
  if (record === undefined || snapshot === undefined) return null;

  if (snapshot.status === "offline" || snapshot.status === "authentication_failed" || snapshot.status === "host_key_error") {
    return (
      <Panel className="preview-server">
        <div className="preview-server__head">
          <StatusDot status={dotStatusFromServerStatus(snapshot.status)} ring label={snapshot.status} />
          <span className="preview-server__name">{record.display_name}</span>
          <span className="preview-server__endpoint mono">
            {record.username ?? "root"}@{record.ssh_host}
          </span>
        </div>
        <ErrorPanel
          title={t.tabs.serverUnreachable}
          detail={snapshot.errors["gpu"] ?? "connection lost"}
        />
      </Panel>
    );
  }

  const visible = snapshot.gpus.slice(0, MAX_LANES);
  const hidden = snapshot.gpus.length - visible.length;
  const hero = snapshot.gpus[0];

  return (
    <Panel className="preview-server">
      <div className="preview-server__head">
        <StatusDot status={dotStatusFromServerStatus(snapshot.status)} label={snapshot.status} />
        <span className="preview-server__name">{record.display_name}</span>
        <span className="preview-server__endpoint mono">
          {record.username ?? "root"}@{record.ssh_host}
          {record.port !== null ? `:${record.port}` : ""}
        </span>
        <span className="preview-server__platform">
          {snapshot.gpus[0]?.name ?? "no GPU"} ×{snapshot.gpus.length} ·{" "}
          {snapshot.system?.os_pretty ?? "unknown OS"}
        </span>
        <Chip tone="neutral" mono>
          {formatRelative(snapshot.generated_at, now)}
        </Chip>
      </div>

      <div className="preview-server__band">
        <SegmentedMeter value={snapshot.cpu?.percent ?? null} aria-label="CPU" />
        <SegmentedMeter value={snapshot.memory?.percent ?? null} aria-label="RAM" />
        {snapshot.storage.some((mount) => (mount.percent ?? 0) >= 80) && (
          <Chip tone="warn">disk &gt; 80%</Chip>
        )}
      </div>

      <div className="preview-server__lanes">
        {visible.map((gpu) => (
          <GpuLane
            key={gpu.index}
            gpu={gpu}
            owners={gpuOwners(gpu, snapshot.gpu_processes)}
          />
        ))}
        {hidden > 0 && <Chip mono>+{hidden} more</Chip>}
      </div>

      {hero !== undefined && (
        <Section title={t.preview.gpuDetail} meta={tf(t.preview.historyMeta, { n: 120 })}>
          <GpuLaneExpanded
            gpu={hero}
            utilizationHistory={fixtureGpuHistory(serverId, hero.index).utilization}
            vramHistory={fixtureGpuHistory(serverId, hero.index).vram}
            owners={gpuOwners(hero, snapshot.gpu_processes)}
          >
            {snapshot.gpu_processes
              .filter(
                (proc) =>
                  (hero.uuid !== null && proc.gpu_uuid === hero.uuid) ||
                  (hero.uuid === null && proc.gpu_index === hero.index),
              )
              .slice(0, 3)
              .map((proc) => (
              <div key={proc.pid} className="preview-proc mono">
                <span className="tnum">{proc.pid}</span>
                <span>{proc.user ?? "?"}</span>
                <span className="preview-proc__cmd">{proc.command ?? proc.process_name ?? "?"}</span>
                <span className="tnum">
                  {proc.used_memory_b !== null ? `${Math.round(proc.used_memory_b / 1e9)} GB` : "N/A"}
                </span>
              </div>
            ))}
          </GpuLaneExpanded>
        </Section>
      )}
    </Panel>
  );
}

/**
 * Design preview over fixture data — the only consumer of src/data/fixtures.
 * Exercises panels, meters, lanes, sparklines, dots, chips, and async states.
 */
export function PreviewPage() {
  const t = useT();
  return (
    <div className="preview-page">
      <p className="preview-page__note micro-label">{t.preview.note}</p>
      {fixtureServers.map((server) => (
        <PreviewServer key={server.server_id} serverId={server.server_id} />
      ))}
      <div className="preview-page__states">
        <Panel>
          <Section title={t.preview.asyncStates}>
            <div className="preview-page__states-row">
              <Skeleton width={180} height={12} />
              <Skeleton width={120} height={22} radius="6px" />
              <Spinner />
              <EmptyState title={t.overview.noGpusReported} hint={t.preview.noGpusHint} />
            </div>
          </Section>
        </Panel>
      </div>
    </div>
  );
}

/**
 * Server detail frame — every async surface state from DESIGN.md "States":
 *
 * - loading: shape-matched skeleton (no shimmer);
 * - snapshot fetch error / offline / timeout / auth-failed / host-key:
 *   ErrorPanel variant that preserves identity (status dot + display name +
 *   endpoint) and offers retry;
 * - stale: values desaturated + localized "stale · Xs ago" chip;
 * - live: the selected tab's sections.
 *
 * Telemetry arrives via store.snapshots[serverId]; per-GPU history via
 * store.history. In dev/preview (no backend), a fixture snapshot is rendered
 * only while the store has no live entry for the id — real data always wins.
 */

import { useEffect, useSyncExternalStore, type ReactNode } from "react";
import {
  Chip,
  ErrorPanel,
  Panel,
  Skeleton,
  StatusDot,
  dotStatusFromServerStatus,
} from "../../design";
import { fixtureGpuHistory, fixtureSnapshots } from "../../data/fixtures";
import { DEFAULT_TAB } from "../../shell/routes";
import { useConsoleStore } from "../../store/consoleStore";
import type { ServerRecord, ServerSnapshot, ServerStatus } from "../../types/models";
import { useNow } from "../../utils/clock";
import { cx } from "../../utils/cx";
import { isSectionHidden, getHiddenSections, subscribeSections } from "../../utils/sections";
import { serverEndpoint } from "../../utils/telemetry";
import { useRelative, useT } from "../../i18n";
import { GpusTab } from "./GpusTab";
import { NetworkTab } from "./NetworkTab";
import { OverviewTab } from "./OverviewTab";
import { ProcessesTab } from "./ProcessesTab";
import { StorageTab } from "./StorageTab";
import { SystemTab } from "./SystemTab";
import "./server-page.css";

/** Statuses that replace the telemetry canvas with an identity-preserving error. */
const UNREACHABLE_STATUSES: readonly ServerStatus[] = [
  "offline",
  "timeout",
  "authentication_failed",
  "host_key_error",
];

function firstErrorDetail(snapshot: ServerSnapshot): string | null {
  for (const detail of Object.values(snapshot.errors)) {
    if (detail !== "") return detail;
  }
  return null;
}

function IdentityLine({
  record,
  displayName,
  status,
}: {
  record: ServerRecord | undefined;
  displayName: string;
  status: ServerStatus;
}) {
  const t = useT();
  return (
    <div className="srv-identity">
      <StatusDot status={dotStatusFromServerStatus(status)} ring label={status} />
      <span className="srv-identity__name">{displayName}</span>
      {record !== undefined && (
        <span className="srv-identity__endpoint mono">{serverEndpoint(record)}</span>
      )}
      <Chip tone="neutral" mono>
        {t.status[status]}
      </Chip>
    </div>
  );
}

function LoadingBody() {
  return (
    <Panel>
      <div className="srv-loading">
        <Skeleton height={26} width={220} radius="6px" />
        <Skeleton height={14} />
        <Skeleton height={14} width="70%" />
        <Skeleton height={92} radius="8px" />
        <Skeleton height={92} radius="8px" />
      </div>
    </Panel>
  );
}

export function ServerPage({ serverId }: { serverId: string }) {
  const t = useT();
  const relative = useRelative();
  const storeSnapshot = useConsoleStore((state) => state.snapshots[serverId]);
  const storeError = useConsoleStore((state) => state.snapshotErrors[serverId]);
  const storedStatus = useConsoleStore((state) => state.statuses[serverId]);
  const record = useConsoleStore((state) =>
    state.servers.find((item) => item.server_id === serverId),
  );
  const route = useConsoleStore((state) => state.route);
  const setTab = useConsoleStore((state) => state.setTab);
  const storeHistory = useConsoleStore((state) => state.history.get(serverId));
  const loadSnapshot = useConsoleStore((state) => state.loadSnapshot);
  useSyncExternalStore(subscribeSections, getHiddenSections, getHiddenSections);

  // Fixture fallback for #/preview and offline development: only while the
  // store has no live snapshot for this id.
  const fixtureSnapshot = storeSnapshot === undefined ? fixtureSnapshots[serverId] : undefined;
  const snapshot = storeSnapshot ?? fixtureSnapshot;

  useEffect(() => {
    if (storeSnapshot === undefined && storeError === undefined && fixtureSnapshot === undefined) {
      void loadSnapshot(serverId);
    }
  }, [serverId, storeSnapshot, storeError, fixtureSnapshot, loadSnapshot]);

  const now = useNow();
  const tab = route.name === "server" && route.serverId === serverId ? route.tab : DEFAULT_TAB;
  // Deep link to a hidden section (#/server/{id}/{hiddenTab}) renders as Overview
  // and the route is rewritten once below; Overview is never hidden → no loop.
  const effectiveTab = isSectionHidden(tab) ? DEFAULT_TAB : tab;

  useEffect(() => {
    if (route.name === "server" && route.serverId === serverId && isSectionHidden(route.tab)) {
      setTab(DEFAULT_TAB);
    }
  }, [route, serverId, setTab]);
  const status: ServerStatus = snapshot?.status ?? storedStatus ?? "unknown";
  const displayName = record?.display_name ?? snapshot?.system?.hostname ?? serverId;

  const resolveSeries = (gpuIndex: number) =>
    storeHistory?.get(gpuIndex) ??
    (fixtureSnapshot !== undefined ? fixtureGpuHistory(serverId, gpuIndex) : undefined);

  let body: ReactNode;
  if (snapshot === undefined) {
    body =
      storeError === undefined ? (
        <LoadingBody />
      ) : (
        <Panel>
          <ErrorPanel
            title={t.tabs.snapshotUnavailable}
            detail={storeError}
            retryLabel={t.common.retry}
            onRetry={() => void loadSnapshot(serverId)}
          />
        </Panel>
      );
  } else if (UNREACHABLE_STATUSES.includes(status)) {
    body = (
      <Panel>
        <ErrorPanel
          title={t.tabs.serverUnreachable}
          detail={firstErrorDetail(snapshot) ?? t.status[status]}
          retryLabel={t.common.retry}
          onRetry={() => void loadSnapshot(serverId)}
        />
      </Panel>
    );
  } else {
    const stale = snapshot.stale || status === "degraded";
    const staleSeconds =
      stale && snapshot.generated_at !== null
        ? Math.max(0, Math.round((now - Date.parse(snapshot.generated_at)) / 1000))
        : null;
    body = (
      <div className={cx("srv-body", stale && "srv-body--stale")}>
        {stale && staleSeconds !== null && (
          <div className="srv-strip">
            <Chip tone="warn" mono>
              {`${t.status.stale} · ${relative(snapshot.generated_at)}`}
            </Chip>
          </div>
        )}
        {effectiveTab === "overview" && <OverviewTab snapshot={snapshot} />}
        {effectiveTab === "gpus" && <GpusTab snapshot={snapshot} resolveSeries={resolveSeries} />}
        {effectiveTab === "processes" && (
          <ProcessesTab snapshot={snapshot} serverId={serverId} serverName={displayName} />
        )}
        {effectiveTab === "system" && <SystemTab snapshot={snapshot} />}
        {effectiveTab === "storage" && <StorageTab snapshot={snapshot} />}
        {effectiveTab === "network" && <NetworkTab snapshot={snapshot} />}
      </div>
    );
  }

  const showIdentity =
    snapshot === undefined || (snapshot !== undefined && UNREACHABLE_STATUSES.includes(status));

  return (
    <div className="srv-page">
      {showIdentity && (
        <IdentityLine record={record} displayName={displayName} status={status} />
      )}
      {body}
    </div>
  );
}

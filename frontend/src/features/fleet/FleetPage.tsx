/**
 * Fleet page — the product's face: a compact aggregate header + a vertical
 * stack of machine panels (never a table, never a card grid). All async states
 * are covered: loading skeleton, error + retry, empty, per-panel offline /
 * stale / disabled. `preview` renders fixture data for design review without a
 * backend (also active on #/preview; the shell currently routes that hash to
 * PreviewPage, so the prop is the reliable entry point).
 */

import { useState } from "react";
import {
  Button,
  Chip,
  EmptyState,
  ErrorPanel,
  Panel,
  Skeleton,
} from "../../design";
import { useConsoleStore } from "../../store/consoleStore";
import type { FleetEntry, FleetSummary } from "../../types/models";
import { cx } from "../../utils/cx";
import { useNow } from "../../utils/clock";
import { fixtureFleetSummary, fixtureSnapshots } from "../../data/fixtures";
import { tf, useT, type Dict } from "../../i18n";
import { FleetMachinePanel } from "./FleetMachinePanel";
import { sortFleetEntries } from "./fleetSort";
import "./fleet.css";

const PREVIEW_HASH = "#/preview";

type SortKey = "name" | "status" | "gpus";

const SORT_OPTIONS = [
  { key: "name", defaultDirection: "asc" },
  { key: "status", defaultDirection: "asc" },
  { key: "gpus", defaultDirection: "desc" },
] as const satisfies ReadonlyArray<{
  key: SortKey;
  defaultDirection: "asc" | "desc";
}>;

function aggregateLine(t: Dict, entries: FleetEntry[]): string {
  const online = entries.filter((entry) => entry.status === "online").length;
  const gpus = entries.reduce((sum, entry) => sum + entry.gpu_count, 0);
  const free = entries.reduce((sum, entry) => sum + entry.gpu_free, 0);
  return tf(t.fleet.aggregate, { servers: entries.length, online, gpus, free });
}

function FleetSkeletonPanel() {
  return (
    <Panel className="fleet-skeleton">
      <div className="fleet-skeleton__row">
        <Skeleton width={10} height={10} radius="5px" />
        <Skeleton width={140} height={14} radius="5px" />
        <div className="fleet-skeleton__spacer" />
        <Skeleton width={170} height={11} />
      </div>
      <Skeleton width={110} height={10} />
      <div className="fleet-skeleton__row">
        <Skeleton width={28} height={8} />
        <Skeleton width={170} height={10} />
        <Skeleton width={28} height={8} />
        <Skeleton width={170} height={10} />
      </div>
      <Skeleton width="100%" height={10} />
      <Skeleton width="64%" height={10} />
    </Panel>
  );
}

export interface FleetPageProps {
  /** Render fixture data instead of live store telemetry (design preview). */
  preview?: boolean;
}

export function FleetPage({ preview = false }: FleetPageProps) {
  const fleetSummary = useConsoleStore((state) => state.fleetSummary);
  const fleetError = useConsoleStore((state) => state.fleetError);
  const snapshots = useConsoleStore((state) => state.snapshots);
  const fleetSort = useConsoleStore((state) => state.fleetSort);
  const setFleetSort = useConsoleStore((state) => state.setFleetSort);
  const loadFleet = useConsoleStore((state) => state.loadFleet);
  const navigate = useConsoleStore((state) => state.navigate);
  const t = useT();
  const now = useNow();

  const [previewHash] = useState(() => window.location.hash === PREVIEW_HASH);
  const useFixtures = (preview || previewHash) && fleetSummary === null;

  const source: FleetSummary | null = useFixtures ? fixtureFleetSummary : fleetSummary;
  const snapshotFor = (entry: FleetEntry) =>
    useFixtures ? fixtureSnapshots[entry.server_id] : snapshots[entry.server_id];

  const entries = source?.servers ?? [];
  const sorted = sortFleetEntries(entries, fleetSort);

  let body;
  if (source === null) {
    if (fleetError !== null) {
      body = (
        <ErrorPanel
          title={t.fleet.error}
          detail={fleetError}
          onRetry={() => void loadFleet()}
        />
      );
    } else {
      // Loading (or not yet connected): shape-matched skeleton panels.
      body = (
        <div className="fleet-stack" aria-label={t.fleet.loadingAria}>
          <FleetSkeletonPanel />
          <FleetSkeletonPanel />
          <FleetSkeletonPanel />
        </div>
      );
    }
  } else if (entries.length === 0) {
    body = (
      <EmptyState
        title={t.fleet.empty}
        hint={t.fleet.emptyHint}
        action={
          <Button variant="primary" onClick={() => navigate("#/settings")}>
            {t.fleet.openSettings}
          </Button>
        }
      />
    );
  } else {
    body = (
      <div className="fleet-stack">
        {sorted.map((entry) => (
          <FleetMachinePanel
            key={entry.server_id}
            entry={entry}
            snapshot={snapshotFor(entry)}
            now={now}
          />
        ))}
      </div>
    );
  }

  const sortLabels: Record<SortKey, string> = {
    name: t.fleet.sortName,
    status: t.fleet.sortStatus,
    gpus: t.fleet.sortGpus,
  };

  return (
    <div className="fleet-page">
      <header className="fleet-head">
        {source !== null ? (
          <span className="fleet-head__aggregate mono tnum">
            {aggregateLine(t, entries)}
          </span>
        ) : (
          <Skeleton width={230} height={12} />
        )}
        {useFixtures && <Chip tone="accent" mono>{t.fleet.fixtureChip}</Chip>}
        <div className="fleet-head__spacer" />
        <div className="fleet-sort" role="group" aria-label={t.fleet.sortAria}>
          <span className="micro-label">{t.fleet.sort}</span>
          {SORT_OPTIONS.map((option) => {
            const active = fleetSort.key === option.key;
            return (
              <button
                key={option.key}
                type="button"
                className={cx("fleet-sort__btn", active && "fleet-sort__btn--active")}
                aria-pressed={active}
                onClick={() => {
                  setFleetSort(
                    active
                      ? {
                          key: option.key,
                          direction: fleetSort.direction === "asc" ? "desc" : "asc",
                        }
                      : { key: option.key, direction: option.defaultDirection },
                  );
                }}
              >
                {sortLabels[option.key]}
                {active && (
                  <span className="fleet-sort__dir">
                    {fleetSort.direction === "asc" ? "↑" : "↓"}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </header>
      {body}
    </div>
  );
}

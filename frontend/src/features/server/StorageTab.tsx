/**
 * Storage tab — one row per mount, sorted by usage (full first, unreported
 * last). The meter uses the storage thresholds (warn 80, crit 90), not the
 * generic utilization ones.
 */

import { Chip, EmptyState, Panel, Section, SegmentedMeter, meterStateForValue } from "../../design";
import { tf, useT } from "../../i18n";
import type { ServerSnapshot } from "../../types/models";
import { formatBytes, formatBytesPair } from "../../utils/format";
import "./server-page.css";

const STORAGE_WARN = 80;
// df's capacity includes ext4 reserved blocks, so a comfortable disk can
// read 85-90%: only >=95% is genuinely critical.
const STORAGE_CRIT = 95;

export function StorageTab({ snapshot }: { snapshot: ServerSnapshot }) {
  const t = useT();
  const mounts = [...snapshot.storage].sort(
    (a, b) => (b.percent ?? -1) - (a.percent ?? -1),
  );

  if (mounts.length === 0) {
    return (
      <Panel>
        <Section title={t.tabs.storage}>
          <EmptyState
            title={t.storage.empty}
            hint={snapshot.errors.storage ?? t.storage.emptyHint}
          />
        </Section>
      </Panel>
    );
  }

  return (
    <Panel>
      <Section
        title={t.tabs.storage}
        meta={tf(
          mounts.length === 1 ? t.storage.mountsOne : t.storage.mountsMany,
          { n: mounts.length },
        )}
      >
        <div className="srv-mounts">
          {mounts.map((mount) => (
            <div key={`${mount.device}-${mount.mount}`} className="srv-mount">
              <span className="srv-mount__id">
                <span className="srv-mount__device mono">{mount.device}</span>
                {mount.fstype !== null && <Chip mono>{mount.fstype}</Chip>}
              </span>
              <span className="srv-mount__path mono">{mount.mount}</span>
              <SegmentedMeter
                className="srv-mount__meter"
                value={mount.percent}
                state={
                  mount.percent === null
                    ? "ok"
                    : meterStateForValue(mount.percent, STORAGE_WARN, STORAGE_CRIT)
                }
                valueText={formatBytesPair(mount.used_b, mount.total_b)}
                aria-label={tf(t.storage.usageOf, { mount: mount.mount })}
              />
              <span className="srv-mount__free mono tnum">
                {tf(t.storage.free, { size: formatBytes(mount.free_b) })}
              </span>
            </div>
          ))}
        </div>
      </Section>
    </Panel>
  );
}

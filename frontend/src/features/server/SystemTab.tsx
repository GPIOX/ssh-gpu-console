/**
 * System tab — quiet definition grid over SystemInfo. Uptime stays live: the
 * snapshot reports seconds-since-boot at generation time and the shared 1Hz
 * clock advances it, so the label never goes stale.
 */

import { EmptyState, Panel, Section } from "../../design";
import { tf, useT } from "../../i18n";
import type { ServerSnapshot, SystemInfo } from "../../types/models";
import { useNow } from "../../utils/clock";
import { formatUptime } from "../../utils/format";
import { cx } from "../../utils/cx";
import "./server-page.css";

function liveUptime(uptimeS: number | null, generatedAt: string | null, now: number): number | null {
  if (uptimeS === null) return null;
  if (generatedAt === null) return uptimeS;
  const generated = Date.parse(generatedAt);
  if (!Number.isFinite(generated)) return uptimeS;
  return uptimeS + Math.max(0, (now - generated) / 1000);
}

function coresText(t: ReturnType<typeof useT>, system: SystemInfo): string {
  const physical = system.cores_physical;
  const logical = system.cores_logical;
  if (physical === null && logical === null) return t.common.na;
  if (physical === null) {
    return logical === null ? t.common.na : tf(t.system.logicalCount, { n: logical });
  }
  if (logical === null) return tf(t.overview.physical, { n: physical });
  return tf(t.system.coresPair, { physical, logical });
}

export function SystemTab({ snapshot }: { snapshot: ServerSnapshot }) {
  const t = useT();
  const now = useNow();
  const system = snapshot.system;

  if (system === null) {
    return (
      <Panel>
        <Section title={t.tabs.system}>
          <EmptyState
            title={t.system.empty}
            hint={snapshot.errors.system ?? t.system.emptyHint}
          />
        </Section>
      </Panel>
    );
  }

  const rows: Array<{ label: string; value: string; mono?: boolean }> = [
    { label: t.system.hostname, value: system.hostname ?? t.common.na, mono: true },
    { label: t.system.os, value: system.os_pretty ?? t.common.na },
    { label: t.system.kernel, value: system.kernel ?? t.common.na, mono: true },
    {
      label: t.system.uptime,
      value: formatUptime(liveUptime(system.uptime_s, snapshot.generated_at, now)),
    },
    { label: t.system.cpuModel, value: system.cpu_model ?? t.common.na },
    { label: t.system.cores, value: coresText(t, system) },
    { label: t.system.driver, value: system.driver_version ?? t.common.na, mono: true },
  ];

  return (
    <Panel>
      <Section title={t.tabs.system} meta={system.hostname ?? undefined}>
        <dl className="srv-defs">
          {rows.map((row) => (
            <div key={row.label} className="srv-defs__row">
              <dt className="micro-label">{row.label}</dt>
              <dd className={cx(row.mono === true && "mono", "tnum")}>{row.value}</dd>
            </div>
          ))}
        </dl>
      </Section>
    </Panel>
  );
}

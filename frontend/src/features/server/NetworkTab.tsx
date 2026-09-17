/**
 * Network tab — one row per interface. Rates are null until the server has
 * two counter samples; null renders "—", never a fabricated zero.
 */

import { EmptyState, Panel, Section } from "../../design";
import { tf, useT } from "../../i18n";
import type { ServerSnapshot } from "../../types/models";
import { formatBps, formatBytes } from "../../utils/format";
import "./server-page.css";

function rateText(bps: number | null): string {
  return bps === null ? "—" : formatBps(bps);
}

export function NetworkTab({ snapshot }: { snapshot: ServerSnapshot }) {
  const t = useT();
  if (snapshot.network.length === 0) {
    return (
      <Panel>
        <Section title={t.tabs.network}>
          <EmptyState
            title={t.network.empty}
            hint={snapshot.errors.network ?? t.network.emptyHint}
          />
        </Section>
      </Panel>
    );
  }

  return (
    <Panel>
      <Section
        title={t.tabs.network}
        meta={tf(
          snapshot.network.length === 1 ? t.network.ifacesOne : t.network.ifacesMany,
          { n: snapshot.network.length },
        )}
      >
        <div className="srv-ifaces">
          {snapshot.network.map((iface) => (
            <div key={iface.name} className="srv-iface">
              <span className="srv-iface__name mono">{iface.name}</span>
              <span className="srv-iface__ip mono">{iface.ip ?? "—"}</span>
              <span className="srv-iface__rate mono tnum">
                {tf(t.overview.rx, { rate: rateText(iface.rx_bps) })}
              </span>
              <span className="srv-iface__rate mono tnum">
                {tf(t.overview.tx, { rate: rateText(iface.tx_bps) })}
              </span>
              <span className="srv-iface__total mono tnum">
                {tf(t.network.inTotal, {
                  size: formatBytes(iface.rx_total_b),
                  sizeOut: formatBytes(iface.tx_total_b),
                })}
              </span>
            </div>
          ))}
        </div>
      </Section>
    </Panel>
  );
}

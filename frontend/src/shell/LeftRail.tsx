import { SquaresFour, Browsers, ArrowsLeftRight, GearSix } from "@phosphor-icons/react";
import { useConsoleStore } from "../store/consoleStore";
import { StatusDot, dotStatusFromServerStatus } from "../design";
import { cx } from "../utils/cx";
import { useT } from "../i18n";
import { FLEET_HASH, WORKSPACE_HASH, TRANSFERS_HASH } from "./routes";

/** 216px left rail: brand mark, FLEET, live server list, SETTINGS at bottom. */
export function LeftRail() {
  const t = useT();
  const route = useConsoleStore((state) => state.route);
  const servers = useConsoleStore((state) => state.servers);
  const serversLoading = useConsoleStore((state) => state.serversLoading);
  const fleetSummary = useConsoleStore((state) => state.fleetSummary);
  const statuses = useConsoleStore((state) => state.statuses);
  const navigate = useConsoleStore((state) => state.navigate);

  const entries = new Map(fleetSummary?.servers.map((entry) => [entry.server_id, entry]) ?? []);
  const rows = servers.map((server) => {
    const entry = entries.get(server.server_id);
    return {
      id: server.server_id,
      name: entry?.display_name ?? server.display_name,
      enabled: server.enabled && entry?.enabled !== false,
      status: entry?.status ?? statuses[server.server_id] ?? "unknown",
    };
  });

  return (
    <aside className="rail">
      <div className="rail__brand">
        <span className="rail__mark" aria-hidden="true" />
        <span className="rail__brand-name micro-label">GPU Console</span>
      </div>

      <nav className="rail__nav" aria-label="Primary">
        <button
          type="button"
          className={cx("rail__item", route.name === "fleet" && "rail__item--active")}
          onClick={() => navigate(FLEET_HASH)}
        >
          <SquaresFour size={16} />
          <span className="rail__item-text">{t.nav.fleet}</span>
        </button>

        <button
          type="button"
          className={cx("rail__item", route.name === "workspace" && "rail__item--active")}
          onClick={() => navigate(WORKSPACE_HASH)}
        >
          <Browsers size={16} />
          <span className="rail__item-text">{t.nav.workspace}</span>
        </button>

        <button
          type="button"
          className={cx("rail__item", route.name === "transfers" && "rail__item--active")}
          onClick={() => navigate(TRANSFERS_HASH)}
        >
          <ArrowsLeftRight size={16} />
          <span className="rail__item-text">{t.nav.transfers}</span>
        </button>

        <div className="rail__group-label micro-label">{t.nav.servers}</div>
        {rows.map((row) => (
          <button
            key={row.id}
            type="button"
            className={cx(
              "rail__item",
              "rail__item--server",
              route.name === "server" && route.serverId === row.id && "rail__item--active",
              !row.enabled && "rail__item--disabled",
            )}
            title={row.name}
            onClick={() => navigate(`#/server/${encodeURIComponent(row.id)}/overview`)}
          >
            <StatusDot
              status={dotStatusFromServerStatus(row.status)}
              label={row.status}
              className="rail__dot"
            />
            <span className="rail__item-text">{row.name}</span>
          </button>
        ))}
        {rows.length === 0 && (
          <div className="rail__empty">{serversLoading ? "Loading…" : "No servers"}</div>
        )}
      </nav>

      <div className="rail__bottom">
        <button
          type="button"
          className={cx("rail__item", route.name === "settings" && "rail__item--active")}
          onClick={() => navigate("#/settings")}
        >
          <GearSix size={16} />
          <span className="rail__item-text">{t.nav.settings}</span>
        </button>
      </div>
    </aside>
  );
}

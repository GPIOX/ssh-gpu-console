/**
 * Settings — the quiet utility page (Fleet is the star). Two sections:
 * 1. Server registry: a dense semantic table (name, endpoint, tags, enabled
 *    chip, live status dot) with Add/Edit via the onboarding dialog and a named
 *    destructive Remove confirmation. Per-machine testing and enablement stay
 *    in Fleet's ⋯ menus.
 * 2. Preferences: honest app info — API base, a one-shot backend health probe
 *    (fetched once on open, never polled on a timer), and a note that
 *    telemetry is memory-only. No fake preference toggles.
 */

import { useEffect, useState, useSyncExternalStore } from "react";
import { Desktop, Moon, PencilSimple, PottedPlant, Sun, Trash } from "@phosphor-icons/react";
import {
  Button,
  Chip,
  Dialog,
  EmptyState,
  ErrorPanel,
  Menu,
  Panel,
  Section,
  Skeleton,
  Spinner,
  StatusDot,
  dotStatusFromServerStatus,
  type MenuItem,
} from "../../design";
import { API_BASE, api, type HealthInfo } from "../../services/api";
import { useConsoleStore } from "../../store/consoleStore";
import type { ServerRecord } from "../../types/models";
import { setLocale, tf, useLocale, useT, type Locale } from "../../i18n";
import {
  getHiddenSections,
  subscribeSections,
  toggleSection,
  TOGGLEABLE_SECTIONS,
} from "../../utils/sections";
import {
  getThemePreference,
  setThemePreference,
  subscribeTheme,
  type ThemePreference,
} from "../../utils/theme";
import { ServerDialog } from "./ServerDialog";
import "./settings.css";

/** `user@host:port` from a registry record (absent parts omitted). */
function endpointOf(record: ServerRecord): string {
  const user = record.username !== null ? `${record.username}@` : "";
  const port = record.port !== null ? `:${record.port}` : "";
  return `${user}${record.ssh_host}${port}`;
}

type DialogTarget = { mode: "add" } | { mode: "edit"; server: ServerRecord } | null;

export function SettingsPage() {
  const servers = useConsoleStore((state) => state.servers);
  const serversLoading = useConsoleStore((state) => state.serversLoading);
  const serversError = useConsoleStore((state) => state.serversError);
  const statuses = useConsoleStore((state) => state.statuses);
  const loadServers = useConsoleStore((state) => state.loadServers);

  const [dialog, setDialog] = useState<DialogTarget>(null);
  const [removeTarget, setRemoveTarget] = useState<ServerRecord | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  // One-shot backend health probe on open — explicitly no polling timer.
  const themePreference = useSyncExternalStore(
    subscribeTheme,
    getThemePreference,
    getThemePreference,
  );
  const hiddenSections = useSyncExternalStore(
    subscribeSections,
    getHiddenSections,
    getHiddenSections,
  );
  const t = useT();
  const locale = useLocale();

  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [healthLoading, setHealthLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .getHealth()
      .then((info) => {
        if (cancelled) return;
        setHealth(info);
        setHealthError(null);
      })
      .catch((cause) => {
        if (cancelled) return;
        setHealthError(cause instanceof Error ? cause.message : "health probe failed");
      })
      .finally(() => {
        if (!cancelled) setHealthLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const refreshRegistry = () => {
    const store = useConsoleStore.getState();
    void store.loadServers();
    void store.loadFleet();
  };

  const removeServer = async (): Promise<void> => {
    if (removeTarget === null) return;
    setRemoving(true);
    setRemoveError(null);
    try {
      await api.deleteServer(removeTarget.server_id);
      setRemoveTarget(null);
      refreshRegistry();
    } catch (cause) {
      setRemoveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setRemoving(false);
    }
  };

  const headAdd = (
    <Button variant="primary" onClick={() => setDialog({ mode: "add" })}>
      {t.settings.addServer}
    </Button>
  );

  let registryBody;
  if (serversError !== null) {
    registryBody = (
      <Panel>
        <ErrorPanel
          title={t.fleet.error}
          detail={serversError}
          onRetry={() => void loadServers()}
        />
      </Panel>
    );
  } else if (serversLoading && servers.length === 0) {
    registryBody = (
      <Panel>
        <div className="settings-skeleton" aria-label="Loading registry">
          {[0, 1, 2].map((row) => (
            <div key={row} className="settings-skeleton__row">
              <Skeleton width={130} height={13} radius="5px" />
              <Skeleton width={170} height={11} />
              <Skeleton width={64} height={16} radius="8px" />
              <Skeleton width={92} height={11} />
            </div>
          ))}
        </div>
      </Panel>
    );
  } else if (servers.length === 0) {
    registryBody = (
      <Panel>
        <EmptyState
          title={t.fleet.empty}
          hint={t.fleet.emptyHint}
          action={headAdd}
        />
      </Panel>
    );
  } else {
    registryBody = (
      <Panel className="settings-reg">
        <table className="settings-table">
          <thead>
            <tr>
              <th>{t.settings.registryHeaderServer}</th>
              <th>{t.settings.registryHeaderEndpoint}</th>
              <th>{t.settings.registryHeaderTags}</th>
              <th>{t.settings.registryHeaderEnabled}</th>
              <th>{t.settings.registryHeaderStatus}</th>
              <th aria-label="Actions" />
            </tr>
          </thead>
          <tbody>
            {servers.map((record) => {
              const status = statuses[record.server_id] ?? "unknown";
              const dot = dotStatusFromServerStatus(status);
              const items: MenuItem[] = [
                {
                  id: "edit",
                  label: t.common.edit,
                  icon: <PencilSimple size={14} />,
                  onSelect: () => setDialog({ mode: "edit", server: record }),
                },
                {
                  id: "remove",
                  label: t.common.remove,
                  icon: <Trash size={14} />,
                  danger: true,
                  onSelect: () => {
                    setRemoveError(null);
                    setRemoveTarget(record);
                  },
                },
              ];
              return (
                <tr key={record.server_id}>
                  <td className="settings-table__name">{record.display_name}</td>
                  <td className="settings-table__endpoint mono">{endpointOf(record)}</td>
                  <td className="settings-table__tags">
                    {record.tags.length > 0 ? (
                      <span className="settings-table__taglist">
                        {record.tags.map((tag) => (
                          <Chip key={tag} mono>
                            {tag}
                          </Chip>
                        ))}
                      </span>
                    ) : (
                      <span className="settings-table__none">—</span>
                    )}
                  </td>
                  <td>
                    {record.enabled ? (
                      <Chip tone="ok">{t.settings.enabled}</Chip>
                    ) : (
                      <Chip>{t.settings.disabled}</Chip>
                    )}
                  </td>
                  <td className="settings-table__status">
                    <span className="settings-table__statusline">
                      <StatusDot
                        status={dot}
                        ring={dot === "offline" || dot === "error"}
                        label={status}
                      />
                      <span className="settings-table__statusword">
                        {t.status[status]}
                      </span>
                    </span>
                  </td>
                  <td className="settings-table__menu">
                    <Menu
                      items={items}
                      triggerLabel={`${t.settings.registry}: ${record.display_name}`}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    );
  }

  return (
    <div className="settings-page">
      <header className="settings-head">
        <div className="settings-head__spacer" />
        {servers.length > 0 && headAdd}
      </header>

      <Section title={t.settings.registry} meta={tf(t.settings.registered, { n: servers.length })}>
        {registryBody}
      </Section>

      <Section title={t.settings.preferences}>
        <Panel className="settings-prefs">
          <div className="settings-prefs__row">
            <span className="micro-label">{t.settings.appearance}</span>
            <div className="theme-switch" role="radiogroup" aria-label={t.settings.appearance}>
              {(
                [
                  { key: "dark", label: t.settings.themeDark, icon: Moon },
                  { key: "minimal", label: t.settings.themeMinimal, icon: Sun },
                  { key: "warm", label: t.settings.themeWarm, icon: PottedPlant },
                  { key: "system", label: t.settings.themeSystem, icon: Desktop },
                ] as const
              ).map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  role="radio"
                  aria-checked={themePreference === key}
                  className={`theme-switch__opt${themePreference === key ? " theme-switch__opt--active" : ""}`}
                  onClick={() => setThemePreference(key as ThemePreference)}
                >
                  <Icon size={13} weight={themePreference === key ? "fill" : "regular"} />
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="settings-prefs__row">
            <span className="micro-label">{t.settings.language}</span>
            <div className="theme-switch" role="radiogroup" aria-label={t.settings.language}>
              {(
                [
                  { key: "en", label: "English" },
                  { key: "zh", label: "中文" },
                ] as const
              ).map(({ key, label }) => (
                <button
                  key={key}
                  type="button"
                  role="radio"
                  aria-checked={locale === key}
                  className={`theme-switch__opt${locale === key ? " theme-switch__opt--active" : ""}`}
                  onClick={() => setLocale(key as Locale)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <div className="settings-prefs__row">
            <span className="micro-label">{t.settings.sections}</span>
            <div className="theme-switch" role="group" aria-label={t.settings.sections}>
              {TOGGLEABLE_SECTIONS.map((tab) => {
                const visible = !hiddenSections.includes(tab);
                return (
                  <button
                    key={tab}
                    type="button"
                    aria-pressed={visible}
                    className={`theme-switch__opt${visible ? " theme-switch__opt--active" : ""}`}
                    onClick={() => toggleSection(tab)}
                  >
                    {t.tabs[tab]}
                  </button>
                );
              })}
            </div>
          </div>
          <div className="settings-prefs__row">
            <span className="micro-label">API base</span>
            <span className="mono">{API_BASE}</span>
          </div>
          <div className="settings-prefs__row">
            <span className="micro-label">{t.settings.backendHealth}</span>
            {healthLoading ? (
              <span className="settings-prefs__health">
                <Spinner size={12} label="checking backend health" />
                <span className="settings-prefs__muted">{t.settings.checking}</span>
              </span>
            ) : healthError !== null ? (
              <Chip tone="crit" mono title={healthError}>
                {t.settings.unreachable}
              </Chip>
            ) : health !== null ? (
              <span className="settings-prefs__health">
                <Chip tone="ok" mono>
                  {health.status}
                </Chip>
                {health.scheduler_mode !== null && (
                  <span className="settings-prefs__fact mono tnum">
                    {tf(t.settings.scheduler, { mode: health.scheduler_mode })}
                  </span>
                )}
                {health.realtime_clients !== null && (
                  <span className="settings-prefs__fact mono tnum">
                    {tf(t.settings.clients, { n: health.realtime_clients })}
                  </span>
                )}
              </span>
            ) : null}
          </div>
          <p className="settings-prefs__note">{t.settings.memoryNote}</p>
          <p className="settings-prefs__note">{t.settings.trustNote}</p>
        </Panel>
      </Section>

      <ServerDialog
        open={dialog !== null}
        onClose={() => setDialog(null)}
        server={dialog?.mode === "edit" ? dialog.server : null}
      />

      <Dialog
        open={removeTarget !== null}
        onClose={() => setRemoveTarget(null)}
        title={removeTarget === null ? undefined : tf(t.fleet.removeConfirm, { name: removeTarget.display_name })}
      >
        {removeTarget !== null && (
          <>
            <p className="settings-dialog__text">
              {tf(t.fleet.removeTitle, { name: removeTarget.display_name })}{" "}
              (<span className="mono">{endpointOf(removeTarget)}</span>)
            </p>
            {removeError !== null && <p className="field__error">{removeError}</p>}
            <div className="settings-dialog__actions">
              <Button onClick={() => setRemoveTarget(null)} disabled={removing}>
                {t.common.cancel}
              </Button>
              <Button
                variant="primary"
                disabled={removing}
                onClick={() => void removeServer()}
              >
                {tf(t.fleet.removeConfirm, { name: removeTarget.display_name })}
              </Button>
            </div>
            {/* Non-blocking hint (spec item 30): deleting here never revokes
                the console's dedicated direct-transfer keys on other machines. */}
            <p className="settings-prefs__note">{t.fleet.removeDirectHint}</p>
          </>
        )}
      </Dialog>
    </div>
  );
}

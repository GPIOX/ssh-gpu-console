/**
 * Processes tab — the one place a dense semantic table is correct (DESIGN.md).
 * Search (name/user/command/pid), a GPU-only toggle, header-click sorting on
 * CPU/MEM/RSS, a 200-row render cap, and per-row ⋯ menus holding the named
 * terminate/kill actions behind a destructive confirmation dialog. Success is
 * surfaced as a toast-style chip; failures surface inline, mapped from the API
 * detail. The snapshot refreshes after every action.
 */

import { useState } from "react";
import { Button, Chip, Dialog, EmptyState, Menu, Panel, Section, TextInput, type MenuItem } from "../../design";
import { tf, useT } from "../../i18n";
import { api } from "../../services/api";
import { useConsoleStore } from "../../store/consoleStore";
import type { ProcessInfo, ServerSnapshot } from "../../types/models";
import { formatBytes, formatPercent } from "../../utils/format";
import { cx } from "../../utils/cx";
import {
  actionPastTense,
  actionSignalHint,
  confirmLabel,
  describeActionError,
  dialogLead,
  type ProcessAction,
} from "./processActions";
import "./server-page.css";

const ROW_CAP = 200;
type SortKey = "cpu" | "mem" | "rss";

interface Sort {
  key: SortKey;
  direction: "asc" | "desc";
}

interface PendingAction {
  action: ProcessAction;
  process: ProcessInfo;
}

interface Toast {
  tone: "ok" | "crit";
  text: string;
}

function sortValue(proc: ProcessInfo, key: SortKey): number | null {
  if (key === "cpu") return proc.cpu_percent;
  if (key === "mem") return proc.mem_percent;
  return proc.rss_b;
}

function ProcessRow({
  proc,
  onAction,
}: {
  proc: ProcessInfo;
  onAction: (action: ProcessAction, proc: ProcessInfo) => void;
}) {
  const t = useT();
  const items: MenuItem[] = [
    {
      id: "terminate",
      label: t.processes.terminate,
      danger: true,
      onSelect: () => onAction("terminate", proc),
    },
    {
      id: "kill",
      label: t.processes.kill,
      danger: true,
      onSelect: () => onAction("kill", proc),
    },
  ];
  return (
    <tr>
      <td className="mono tnum">{proc.pid}</td>
      <td>{proc.user ?? "—"}</td>
      <td>{proc.name ?? "—"}</td>
      <td className="mono tnum">{formatPercent(proc.cpu_percent, 1)}</td>
      <td className="mono tnum">{formatPercent(proc.mem_percent, 1)}</td>
      <td className="mono tnum">{formatBytes(proc.rss_b)}</td>
      <td className="mono tnum" title={proc.gpu_vram_b !== null ? formatBytes(proc.gpu_vram_b) : undefined}>
        {proc.gpu_indexes.length > 0 ? proc.gpu_indexes.join(", ") : "—"}
      </td>
      <td className="mono">{proc.state ?? "—"}</td>
      <td className="srv-table__cmd mono" title={proc.command ?? undefined}>
        {proc.command ?? "—"}
      </td>
      <td className="srv-table__menu">
        <Menu items={items} triggerLabel={tf(t.processes.rowActions, { pid: proc.pid })} />
      </td>
    </tr>
  );
}

export function ProcessesTab({
  snapshot,
  serverId,
  serverName,
}: {
  snapshot: ServerSnapshot;
  serverId: string;
  serverName: string;
}) {
  const t = useT();
  const loadSnapshot = useConsoleStore((state) => state.loadSnapshot);
  const [query, setQuery] = useState("");
  const [gpuOnly, setGpuOnly] = useState(false);
  const [sort, setSort] = useState<Sort>({ key: "cpu", direction: "desc" });
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [toast, setToast] = useState<Toast | null>(null);

  const filtered = snapshot.processes.filter((proc) => {
    if (gpuOnly && proc.gpu_indexes.length === 0) return false;
    const q = query.trim().toLowerCase();
    if (q === "") return true;
    return [proc.name, proc.user, proc.command, String(proc.pid)]
      .map((part) => part ?? "")
      .join(" ")
      .toLowerCase()
      .includes(q);
  });

  const sorted = [...filtered].sort((a, b) => {
    const av = sortValue(a, sort.key);
    const bv = sortValue(b, sort.key);
    if (av === null && bv === null) return 0;
    if (av === null) return 1; // unreported values sort last, both directions
    if (bv === null) return -1;
    return sort.direction === "desc" ? bv - av : av - bv;
  });

  const visible = sorted.slice(0, ROW_CAP);

  const toggleSort = (key: SortKey): void => {
    setSort((previous) =>
      previous.key === key
        ? { key, direction: previous.direction === "desc" ? "asc" : "desc" }
        : { key, direction: "desc" },
    );
  };

  const dismissDialog = (): void => {
    setPending(null);
    setActionError(null);
  };

  const runAction = async (): Promise<void> => {
    if (pending === null) return;
    const { action, process } = pending;
    try {
      if (action === "terminate") {
        await api.terminateProcess(serverId, process.pid);
      } else {
        await api.killProcess(serverId, process.pid);
      }
      setToast({
        tone: "ok",
        text: tf(t.processes.actionOk, { action: actionPastTense(t, action), pid: process.pid }),
      });
      setActionError(null);
      setPending(null);
      void loadSnapshot(serverId); // refresh telemetry after the action
    } catch (cause) {
      setActionError(describeActionError(t, cause)); // surfaced inline, dialog stays open
    }
  };

  const meta =
    filtered.length > ROW_CAP
      ? tf(t.processes.showing, { n: ROW_CAP, m: filtered.length })
      : tf(filtered.length === 1 ? t.processes.countOne : t.processes.countMany, {
          n: filtered.length,
        });

  // Sort-header labels are plain column names; the sort arrow is a symbol.
  const sortLabels: Record<SortKey, string> = {
    cpu: t.processes.colCpu,
    mem: t.processes.colMem,
    rss: t.processes.colRss,
  };

  return (
    <div className="srv-stack">
      {toast !== null && (
        <div className="srv-strip" role="status">
          <Chip tone={toast.tone} mono>
            {toast.text}
          </Chip>
        </div>
      )}
      <Panel>
        <Section
          title={t.tabs.processes}
          meta={snapshot.processes.length > 0 ? meta : undefined}
        >
          {snapshot.processes.length === 0 ? (
            <EmptyState title={t.processes.empty} hint={t.processes.emptyHint} />
          ) : (
            <>
              <div className="srv-controls">
                <TextInput
                  aria-label={t.processes.searchAria}
                  placeholder={t.processes.search}
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                />
                <button
                  type="button"
                  className={cx("srv-toggle", "mono", gpuOnly && "srv-toggle--on")}
                  aria-pressed={gpuOnly}
                  onClick={() => setGpuOnly((value) => !value)}
                >
                  {t.processes.gpuOnly}
                </button>
              </div>
              {visible.length === 0 ? (
                <EmptyState title={t.processes.noMatch} hint={t.processes.noMatchHint} />
              ) : (
                <div className="srv-table-wrap">
                  <table className="srv-table">
                    <thead>
                      <tr>
                        <th scope="col">{t.processes.colPid}</th>
                        <th scope="col">{t.processes.colUser}</th>
                        <th scope="col">{t.processes.colName}</th>
                        {(Object.keys(sortLabels) as SortKey[]).map((key) => (
                          <th
                            key={key}
                            scope="col"
                            aria-sort={
                              sort.key === key
                                ? sort.direction === "asc"
                                  ? "ascending"
                                  : "descending"
                                : undefined
                            }
                          >
                            <button
                              type="button"
                              className="srv-table__sort"
                              onClick={() => toggleSort(key)}
                            >
                              {sortLabels[key]}
                              {sort.key === key ? (sort.direction === "desc" ? " ↓" : " ↑") : ""}
                            </button>
                          </th>
                        ))}
                        <th scope="col">{t.processes.colGpu}</th>
                        <th scope="col">{t.processes.colState}</th>
                        <th scope="col">{t.processes.colCommand}</th>
                        <th scope="col">
                          <span className="srv-visually-hidden">{t.processes.colActions}</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((proc) => (
                        <ProcessRow
                          key={proc.pid}
                          proc={proc}
                          onAction={(action, target) => {
                            setActionError(null);
                            setPending({ action, process: target });
                          }}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </Section>
      </Panel>

      <Dialog
        open={pending !== null}
        onClose={dismissDialog}
        title={
          pending !== null ? confirmLabel(t, pending.action, pending.process.pid) : undefined
        }
      >
        {pending !== null && (
          <div className="srv-confirm">
            <p className="srv-confirm__lead">
              {dialogLead(
                t,
                pending.action,
                pending.process.pid,
                pending.process.name,
                serverName,
              )}
            </p>
            {pending.process.command !== null && (
              <p className="srv-confirm__cmd mono">{pending.process.command}</p>
            )}
            <p className="srv-confirm__hint">{actionSignalHint(t, pending.action)}</p>
            {actionError !== null && (
              <p className="srv-confirm__error" role="alert">
                {actionError}
              </p>
            )}
            <div className="srv-confirm__actions">
              <Button onClick={dismissDialog}>{t.common.cancel}</Button>
              <Button variant="primary" onClick={() => void runAction()}>
                {confirmLabel(t, pending.action, pending.process.pid)}
              </Button>
            </div>
          </div>
        )}
      </Dialog>
    </div>
  );
}

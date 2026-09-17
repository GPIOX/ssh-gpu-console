/**
 * Phase 4E sync dialog: pick a target server → POST sync-plan → review the
 * per-artifact decisions → POST sync. Restrained per DESIGN.md: a plain
 * table, chips only for the action word, backend reasons/warnings verbatim.
 * Confirm stays disabled while UNRESOLVED items exist or the plan is invalid
 * — the backend would refuse the whole sync with 409.
 */

import { useEffect, useState } from "react";
import { Button, Chip, Dialog, Field, Select, type ChipTone } from "../../design";
import { tf, useT, type Dict } from "../../i18n";
import { useConsoleStore } from "../../store/consoleStore";
import { useTransferStore } from "../../store/transferStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import { strategyWord } from "../transfers/TransferJobRow";
import type { ArtifactSyncItem, DistributionState, SyncAction } from "../../types/workspace";

function statusWord(t: Dict, state: DistributionState): string {
  switch (state) {
    case "verified":
      return t.workspace.stateVerified;
    case "missing":
      return t.workspace.stateMissing;
    case "unavailable":
      return t.workspace.stateUnavailable;
    case "syncing":
      return t.workspace.stateSyncing;
    case "declared":
      return t.workspace.stateDeclared;
  }
}

function actionWord(t: Dict, action: SyncAction): string {
  if (action === "transfer") return t.workspace.syncActionTransfer;
  if (action === "unresolved") return t.workspace.syncActionUnresolved;
  return t.workspace.syncActionSkip;
}

function actionTone(action: SyncAction): ChipTone {
  if (action === "transfer") return "accent";
  if (action === "unresolved") return "warn";
  return "neutral";
}

function SyncItemRow({
  item,
  expanded,
  onToggle,
}: {
  item: ArtifactSyncItem;
  expanded: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const serverName = (id: string | null): string =>
    id === null
      ? t.workspace.unknownServer
      : (servers.find((server) => server.server_id === id)?.display_name ?? id);

  return (
    <>
      <tr>
        <td className="ws-sync__artifact">
          <span className="ws-sync__artifact-label">{item.artifact_label}</span>
          {item.action === "transfer" && (
            <button
              type="button"
              className="ws-sync__toggle"
              aria-expanded={expanded}
              aria-label={tf(t.workspace.syncDetailAria, { name: item.artifact_label })}
              onClick={onToggle}
            >
              {expanded ? "▾" : "▸"}
            </button>
          )}
        </td>
        <td className="ws-sync__status">{statusWord(t, item.target_status)}</td>
        <td className="ws-sync__action">
          <Chip tone={actionTone(item.action)}>{actionWord(t, item.action)}</Chip>
          {/* The backend reason is shown in place for unresolved rows. */}
          {item.action === "unresolved" && item.reason !== "" && (
            <p className="ws-sync__reason">{item.reason}</p>
          )}
        </td>
      </tr>
      {item.action === "transfer" && expanded && (
        <tr className="ws-sync__detail">
          <td colSpan={3}>
            <p className="ws-sync__flow mono">
              {serverName(item.source_server_id)}:{item.source_path ?? "?"} → {item.target_path ?? "?"}
            </p>
            {item.strategy_selected !== null && (
              <p className="ws-sync__fact">
                {t.workspace.syncStrategy}: {strategyWord(t, item.strategy_selected)}
              </p>
            )}
            {item.warnings.map((warning) => (
              <p key={warning} className="ws-sync__warning">
                {warning}
              </p>
            ))}
          </td>
        </tr>
      )}
    </>
  );
}

export interface SyncDialogProps {
  open: boolean;
  onClose: () => void;
  projectId: string;
}

export function SyncDialog({ open, onClose, projectId }: SyncDialogProps) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const syncPlan = useWorkspaceStore((state) => state.syncPlan);
  const syncPlanLoading = useWorkspaceStore((state) => state.syncPlanLoading);
  const syncPlanError = useWorkspaceStore((state) => state.syncPlanError);
  const syncRunning = useWorkspaceStore((state) => state.syncRunning);
  const syncError = useWorkspaceStore((state) => state.syncError);
  const buildSyncPlan = useWorkspaceStore((state) => state.buildSyncPlan);
  const syncProject = useWorkspaceStore((state) => state.syncProject);
  const resetSyncPlan = useWorkspaceStore((state) => state.resetSyncPlan);
  const fetchDistribution = useWorkspaceStore((state) => state.fetchDistribution);
  const fetchBatches = useTransferStore((state) => state.fetchBatches);

  const [targetServerId, setTargetServerId] = useState("");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const enabledServers = servers.filter((server) => server.enabled);

  // Fresh dialog state per open: no stale plan from a previous session.
  useEffect(() => {
    if (open) {
      setTargetServerId("");
      setExpanded({});
      resetSyncPlan();
    }
  }, [open, resetSyncPlan]);

  // Choosing a target server (re)builds the plan — the plan is always
  // regenerated server-side, never accepted from the client.
  useEffect(() => {
    if (!open || targetServerId === "") return;
    void buildSyncPlan(projectId, { target_server_id: targetServerId });
  }, [open, targetServerId, projectId, buildSyncPlan]);

  const items = syncPlan?.items ?? [];
  const transferCount = items.filter((item) => item.action === "transfer").length;
  const unresolvedCount = items.filter((item) => item.action === "unresolved").length;
  const planInvalid = syncPlan !== null && (!syncPlan.valid || syncPlan.error !== "");
  const canConfirm =
    syncPlan !== null && !planInvalid && unresolvedCount === 0 && transferCount > 0 && !syncRunning;

  const confirm = async (): Promise<void> => {
    if (targetServerId === "") return;
    try {
      await syncProject(projectId, { target_server_id: targetServerId });
      // Surface the batch and flip the matrix cells to syncing without
      // waiting for the next poll tick.
      void fetchBatches();
      void fetchDistribution(projectId);
      onClose();
    } catch {
      // syncError is recorded in the store and rendered verbatim below.
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title={t.workspace.syncDialogTitle} width={620}>
      <div className="ws-dialog__form">
        <Field
          label={t.transfers.targetServer}
          htmlFor="ws-sync-target"
          hint={enabledServers.length === 0 ? t.transfers.noServers : undefined}
        >
          <Select
            id="ws-sync-target"
            value={targetServerId}
            onChange={(event) => setTargetServerId(event.target.value)}
          >
            {enabledServers.length === 0 && <option value="">—</option>}
            {enabledServers.map((server) => (
              <option key={server.server_id} value={server.server_id}>
                {server.display_name}
              </option>
            ))}
          </Select>
        </Field>

        {syncPlanLoading && <p className="ws-sync__busy">{t.workspace.syncPlanning}</p>}
        {syncPlanError !== null && <p className="field__error">{syncPlanError}</p>}

        {syncPlan !== null && (
          <>
            {planInvalid && <p className="field__error">{syncPlan.error}</p>}
            <div className="ws-sync__scroll">
              <table className="ws-sync__table">
                <thead>
                  <tr>
                    <th>{t.workspace.matrixArtifact}</th>
                    <th>{t.workspace.syncCurrent}</th>
                    <th>{t.workspace.syncAction}</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <SyncItemRow
                      key={item.artifact_id}
                      item={item}
                      expanded={expanded[item.artifact_id] === true}
                      onToggle={() =>
                        setExpanded((current) => ({
                          ...current,
                          [item.artifact_id]: !current[item.artifact_id],
                        }))
                      }
                    />
                  ))}
                </tbody>
              </table>
            </div>
            {unresolvedCount > 0 && <p className="ws-sync__hint">{t.workspace.syncUnresolvedHint}</p>}
          </>
        )}

        {syncError !== null && <p className="field__error">{syncError}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={syncRunning}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" disabled={!canConfirm} onClick={() => void confirm()}>
            {transferCount === 0
              ? t.workspace.syncNothing
              : tf(t.workspace.syncConfirm, { n: transferCount })}
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

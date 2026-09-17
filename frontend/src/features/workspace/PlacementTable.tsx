/**
 * PlacementTable — per-artifact placement rows for a set of artifacts.
 * Each row: server display name (registry), mono remote path, an explicit
 * Inspect action, and the RAM-only inspection result (state chip + file
 * facts). Inspections are never persisted and vanish on reload by design.
 */

import { useState } from "react";
import { Button, Chip, Dialog, Menu, Panel, type ChipTone } from "../../design";
import { tf, useRelative, useT } from "../../i18n";
import { useConsoleStore } from "../../store/consoleStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type {
  ArtifactRecord,
  InspectionState,
  PlacementRecord,
} from "../../types/workspace";
import { formatBytes } from "../../utils/format";
import { useNow } from "../../utils/clock";
import { artifactLabel, kindLabel } from "./shared";
import "./workspace.css";

function stateLabel(t: ReturnType<typeof useT>, state: InspectionState): string {
  switch (state) {
    case "verified":
      return t.workspace.stateVerified;
    case "missing":
      return t.workspace.stateMissing;
    case "unavailable":
      return t.workspace.stateUnavailable;
    case "declared":
      return t.workspace.stateDeclared;
  }
}

function stateTone(state: InspectionState): ChipTone {
  switch (state) {
    case "verified":
      return "ok";
    case "missing":
      return "crit";
    case "unavailable":
      return "warn";
    case "declared":
      return "neutral";
  }
}

export interface PlacementTableProps {
  artifacts: ArtifactRecord[];
  /** Placements of those artifacts only (caller filters). */
  placements: PlacementRecord[];
  onEdit: (placement: PlacementRecord) => void;
  onRemove: (placement: PlacementRecord) => void;
  /** Optional 同步到… entry (transfer prefill); omitted on other callers. */
  onSync?: (placement: PlacementRecord) => void;
}

export function PlacementTable({
  artifacts,
  placements,
  onEdit,
  onRemove,
  onSync,
}: PlacementTableProps) {
  const t = useT();
  const relative = useRelative();
  const now = useNow();
  const servers = useConsoleStore((state) => state.servers);
  const inspections = useWorkspaceStore((state) => state.inspections);
  const inspecting = useWorkspaceStore((state) => state.inspecting);
  const inspectErrors = useWorkspaceStore((state) => state.inspectErrors);
  const inspectPlacement = useWorkspaceStore((state) => state.inspectPlacement);

  const byArtifact = new Map<string, PlacementRecord[]>();
  for (const placement of placements) {
    const list = byArtifact.get(placement.artifact_id) ?? [];
    list.push(placement);
    byArtifact.set(placement.artifact_id, list);
  }

  return (
    <div className="ws-placement-groups">
      {artifacts.map((artifact) => {
        const rows = byArtifact.get(artifact.artifact_id) ?? [];
        return (
          <div key={artifact.artifact_id} className="ws-placement-group">
            <div className="ws-placement-group__head">
              <span className="ws-placement-group__name mono">{artifactLabel(artifact)}</span>
              <Chip>{kindLabel(t, artifact.kind)}</Chip>
            </div>
            {rows.length === 0 ? (
              <p className="ws-hint">{t.workspace.noPlacements}</p>
            ) : (
              rows.map((placement) => {
                const server = servers.find((s) => s.server_id === placement.server_id);
                const inspection = inspections[placement.placement_id];
                const busy = inspecting[placement.placement_id] === true;
                const failure = inspectErrors[placement.placement_id] ?? "";
                return (
                  <Panel key={placement.placement_id} className="ws-placement">
                    <div className="ws-placement__main">
                      <span className="ws-placement__server">
                        {server !== undefined ? server.display_name : placement.server_id}
                        {server === undefined && (
                          <Chip className="ws-placement__unknown">{t.workspace.unknownServer}</Chip>
                        )}
                      </span>
                      <span className="ws-placement__path mono">{placement.remote_path}</span>
                    </div>
                    <div className="ws-placement__side">
                      {inspection !== undefined && (
                        <span className="ws-inspection">
                          <Chip tone={stateTone(inspection.state)} mono>
                            {stateLabel(t, inspection.state)}
                          </Chip>
                          {inspection.file_type !== null && (
                            <span className="ws-inspection__fact mono">
                              {inspection.file_type}
                            </span>
                          )}
                          {inspection.size_b !== null && (
                            <span className="ws-inspection__fact mono tnum">
                              {formatBytes(inspection.size_b)}
                            </span>
                          )}
                          {inspection.file_count !== null && (
                            <span className="ws-inspection__fact mono tnum">
                              {tf(t.workspace.fileCount, { n: inspection.file_count })}
                            </span>
                          )}
                          {inspection.checked_at !== "" && (
                            <span
                              className="ws-inspection__ago mono"
                              title={inspection.checked_at}
                            >
                              {tf(t.workspace.inspectedAgo, {
                                ago: relative(inspection.checked_at, now),
                              })}
                            </span>
                          )}
                        </span>
                      )}
                      {failure !== "" && <span className="field__error">{failure}</span>}
                      <Button
                        onClick={() => void inspectPlacement(placement.placement_id)}
                        disabled={busy}
                      >
                        {busy ? t.workspace.inspecting : t.workspace.inspect}
                      </Button>
                      <span
                        className="ws-menu-hold"
                        onClick={(event) => event.stopPropagation()}
                      >
                        <RowMenu
                          placement={placement}
                          onEdit={onEdit}
                          onRemove={onRemove}
                          onSync={onSync}
                        />
                      </span>
                    </div>
                  </Panel>
                );
              })
            )}
          </div>
        );
      })}
    </div>
  );
}

function RowMenu({
  placement,
  onEdit,
  onRemove,
  onSync,
}: {
  placement: PlacementRecord;
  onEdit: (placement: PlacementRecord) => void;
  onRemove: (placement: PlacementRecord) => void;
  onSync?: (placement: PlacementRecord) => void;
}) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const serverName =
    servers.find((s) => s.server_id === placement.server_id)?.display_name ??
    placement.server_id;
  return (
    <Menu
      items={[
        { id: "edit", label: t.common.edit, onSelect: () => onEdit(placement) },
        {
          id: "remove",
          label: t.common.remove,
          danger: true,
          onSelect: () => onRemove(placement),
        },
        ...(onSync !== undefined
          ? [
              {
                id: "sync",
                label: t.workspace.syncTo,
                onSelect: () => onSync(placement),
              },
            ]
          : []),
      ]}
      triggerLabel={tf(t.workspace.placementActions, { server: serverName })}
    />
  );
}

/** Named destructive confirmation for a placement (metadata-only removal). */
export function PlacementRemoveDialog({
  placement,
  onClose,
}: {
  placement: PlacementRecord | null;
  onClose: () => void;
}) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const deletePlacement = useWorkspaceStore((state) => state.deletePlacement);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (placement === null) return null;
  const serverName =
    servers.find((s) => s.server_id === placement.server_id)?.display_name ??
    placement.server_id;
  const artifactName = artifacts
    .filter((a) => a.artifact_id === placement.artifact_id)
    .map((a) => artifactLabel(a))[0] ?? placement.artifact_id;

  const remove = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await deletePlacement(placement.placement_id);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={tf(t.workspace.removePlacementTitle, { server: serverName })}
    >
      <p className="ws-confirm__text">
        {t.workspace.removePlacementBody} <span className="mono">{artifactName}</span>
      </p>
      {error !== null && <p className="field__error">{error}</p>}
      <div className="ws-dialog__actions">
        <Button onClick={onClose} disabled={busy}>
          {t.common.cancel}
        </Button>
        <Button variant="primary" disabled={busy} onClick={() => void remove()}>
          {t.workspace.removePlacementConfirm}
        </Button>
      </div>
    </Dialog>
  );
}

/**
 * Artifacts tab — shared by the datasets and models tabs, filtered by kind.
 * Rows: kind chip, name:version, immutable chip, placement count, ⋯ menu
 * (Edit / Remove / 同步到… — the transfer entry opens the Transfer Center
 * dialog with this artifact prefilled). 409 rejections (artifact referenced by
 * a project, or placements exist) surface the backend message verbatim.
 */

import { useState } from "react";
import { Button, Chip, Dialog, EmptyState, ErrorPanel, Menu, Panel, Skeleton } from "../../design";
import { openNewTransfer } from "../../store/transferStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ArtifactKind, ArtifactRecord } from "../../types/workspace";
import { useT, tf } from "../../i18n";
import { artifactLabel } from "./shared";
import { ArtifactDialog } from "./ArtifactDialog";
import "./workspace.css";

export function ArtifactsTab({ kind }: { kind: ArtifactKind }) {
  const t = useT();
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const placements = useWorkspaceStore((state) => state.placements);
  const loading = useWorkspaceStore((state) => state.artifactsLoading);
  const error = useWorkspaceStore((state) => state.artifactsError);
  const loadArtifacts = useWorkspaceStore((state) => state.loadArtifacts);
  const deleteArtifact = useWorkspaceStore((state) => state.deleteArtifact);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<ArtifactRecord | null>(null);
  const [removeTarget, setRemoveTarget] = useState<ArtifactRecord | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const rows = artifacts.filter((artifact) => artifact.kind === kind);
  const placementCount = (artifactId: string): number =>
    placements.filter((placement) => placement.artifact_id === artifactId).length;

  const remove = async (): Promise<void> => {
    if (removeTarget === null) return;
    setRemoving(true);
    setRemoveError(null);
    try {
      await deleteArtifact(removeTarget.artifact_id);
      setRemoveTarget(null);
    } catch (cause) {
      // Referenced artifacts are blocked server-side (409) — show why.
      setRemoveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setRemoving(false);
    }
  };

  const add = (
    <Button
      variant="primary"
      onClick={() => {
        setEditTarget(null);
        setDialogOpen(true);
      }}
    >
      {t.workspace.addArtifact}
    </Button>
  );

  let body;
  if (error !== null) {
    body = (
      <Panel>
        <ErrorPanel
          title={t.workspace.noArtifacts}
          detail={error}
          onRetry={() => void loadArtifacts()}
          retryLabel={t.common.retry}
        />
      </Panel>
    );
  } else if (loading && artifacts.length === 0) {
    body = (
      <Panel>
        <div className="ws-skeleton" aria-label={kind}>
          {[0, 1, 2].map((row) => (
            <div key={row} className="ws-skeleton__row">
              <Skeleton width={180} height={13} radius="5px" />
              <Skeleton width={90} height={16} radius="8px" />
              <Skeleton width={80} height={11} />
            </div>
          ))}
        </div>
      </Panel>
    );
  } else if (rows.length === 0) {
    body = (
      <Panel>
        <EmptyState title={t.workspace.noArtifacts} hint={t.workspace.noArtifactsHint} action={add} />
      </Panel>
    );
  } else {
    body = (
      <div className="ws-list">
        {rows.map((artifact) => (
          <Panel key={artifact.artifact_id} className="ws-row">
            <div className="ws-row__main">
              <span className="ws-row__name mono">{artifactLabel(artifact)}</span>
              {artifact.description !== "" && (
                <span className="ws-row__desc">{artifact.description}</span>
              )}
              <span className="ws-row__facts mono tnum">
                {tf(t.workspace.placementsCount, { n: placementCount(artifact.artifact_id) })}
              </span>
            </div>
            <div className="ws-row__tags">
              <Chip>{kind === "model" ? t.workspace.kindModel : t.workspace.kindDataset}</Chip>
              <Chip tone={artifact.immutable ? "cold" : "warn"}>
                {artifact.immutable ? t.workspace.immutableChip : t.workspace.mutableChip}
              </Chip>
            </div>
            <Menu
              items={[
                {
                  id: "edit",
                  label: t.common.edit,
                  onSelect: () => {
                    setEditTarget(artifact);
                    setDialogOpen(true);
                  },
                },
                {
                  id: "remove",
                  label: t.common.remove,
                  danger: true,
                  onSelect: () => {
                    setRemoveError(null);
                    setRemoveTarget(artifact);
                  },
                },
                {
                  id: "sync",
                  label: t.workspace.syncTo,
                  onSelect: () => openNewTransfer({ artifactId: artifact.artifact_id }),
                },
              ]}
              triggerLabel={tf(t.workspace.artifactActions, { name: artifact.name })}
            />
          </Panel>
        ))}
      </div>
    );
  }

  return (
    <section className="ws-tab">
      <header className="ws-tab__head">
        <h2 className="ws-tab__title micro-label">
          {kind === "model" ? t.workspace.tabModels : t.workspace.tabDatasets}
        </h2>
        <div className="ws-tab__spacer" />
        <Button onClick={() => void loadArtifacts()} disabled={loading}>
          {t.workspace.refresh}
        </Button>
        {artifacts.length > 0 && add}
      </header>
      {body}

      <ArtifactDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        kind={kind}
        artifact={editTarget}
      />

      <Dialog
        open={removeTarget !== null}
        onClose={() => setRemoveTarget(null)}
        title={
          removeTarget === null
            ? undefined
            : tf(t.workspace.removeArtifactConfirm, { name: artifactLabel(removeTarget) })
        }
      >
        {removeTarget !== null && (
          <>
            <p className="ws-confirm__text">
              {tf(t.workspace.removeArtifactTitle, { name: artifactLabel(removeTarget) })}{" "}
              {t.workspace.removeArtifactBody}
            </p>
            {removeError !== null && <p className="field__error">{removeError}</p>}
            <div className="ws-dialog__actions">
              <Button onClick={() => setRemoveTarget(null)} disabled={removing}>
                {t.common.cancel}
              </Button>
              <Button variant="primary" disabled={removing} onClick={() => void remove()}>
                {tf(t.workspace.removeArtifactConfirm, { name: artifactLabel(removeTarget) })}
              </Button>
            </div>
          </>
        )}
      </Dialog>
    </section>
  );
}

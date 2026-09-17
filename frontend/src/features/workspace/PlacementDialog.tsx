/**
 * Add/Edit placement dialog. A placement is a DECLARATION only: artifact on a
 * server at a remote path — it never triggers a transfer and never touches
 * remote files. Duplicate (artifact, server, path) triples are rejected
 * server-side (409) and surfaced verbatim.
 */

import { useEffect, useState } from "react";
import { Button, Dialog, Field, Select, TextInput } from "../../design";
import { useT } from "../../i18n";
import { useConsoleStore } from "../../store/consoleStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ArtifactRecord, PlacementRecord } from "../../types/workspace";
import { artifactLabel, kindLabel, remotePathValid } from "./shared";
import "./workspace.css";

export interface PlacementDialogProps {
  open: boolean;
  onClose: () => void;
  /** Edit mode record; null in add mode. */
  placement: PlacementRecord | null;
  /** Artifact choices for the select (project artifacts in ProjectDetail). */
  artifacts: ArtifactRecord[];
  /** Artifact id pinned from context (add mode within one artifact). */
  artifactId?: string;
}

export function PlacementDialog({
  open,
  onClose,
  placement,
  artifacts,
  artifactId,
}: PlacementDialogProps) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const [artifact, setArtifact] = useState(artifactId ?? "");
  const [serverId, setServerId] = useState("");
  const [path, setPath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setArtifact(placement?.artifact_id ?? artifactId ?? "");
    setServerId(placement?.server_id ?? "");
    setPath(placement?.remote_path ?? "");
    setSaving(false);
    setError(null);
  }, [open, placement, artifactId]);

  const submit = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const store = useWorkspaceStore.getState();
      if (placement === null) {
        await store.createPlacement({
          artifact_id: artifact,
          server_id: serverId,
          remote_path: path.trim(),
        });
      } else {
        await store.patchPlacement(placement.placement_id, { remote_path: path.trim() });
      }
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setSaving(false);
    }
  };

  const canSubmit =
    artifact !== "" && serverId !== "" && remotePathValid(path.trim()) && !saving;

  return (
    <Dialog open={open} onClose={onClose} title={placement === null ? t.workspace.addPlacement : t.workspace.editPlacement} width={480}>
      <form
        className="ws-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) void submit();
        }}
      >
        <Field label={t.workspace.matrixArtifact} htmlFor="ws-placement-artifact">
          <Select
            id="ws-placement-artifact"
            value={artifact}
            disabled={placement !== null || artifacts.length === 0}
            onChange={(event) => setArtifact(event.target.value)}
          >
            <option value="">—</option>
            {artifacts.map((option) => (
              <option key={option.artifact_id} value={option.artifact_id}>
                {artifactLabel(option)} · {kindLabel(t, option.kind)}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t.workspace.placementServer} htmlFor="ws-placement-server">
          <Select
            id="ws-placement-server"
            value={serverId}
            disabled={placement !== null || servers.length === 0}
            onChange={(event) => setServerId(event.target.value)}
          >
            <option value="">—</option>
            {servers.map((server) => (
              <option key={server.server_id} value={server.server_id}>
                {server.display_name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t.workspace.placementPath} htmlFor="ws-placement-path">
          <TextInput
            id="ws-placement-path"
            className="mono"
            placeholder={t.workspace.pathPlaceholder}
            value={path}
            onChange={(event) => setPath(event.target.value)}
          />
        </Field>

        {error !== null && <p className="field__error">{error}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={saving}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmit}>
            {saving ? t.dialog.saving : placement === null ? t.workspace.addPlacement : t.dialog.save}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

/**
 * Add/Edit artifact dialog. Kind is chosen on create only (ArtifactPatch has
 * no kind — datasets/models are immutable by default; code may be mutable).
 * The 同步到… transfer flow lands next sprint and is not part of this dialog.
 */

import { useEffect, useState } from "react";
import { Button, Dialog, Field, Select, TextInput } from "../../design";
import { tf, useT } from "../../i18n";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ArtifactCreate, ArtifactKind, ArtifactRecord } from "../../types/workspace";
import { cx } from "../../utils/cx";
import "./workspace.css";

const KINDS: ArtifactKind[] = ["dataset", "model", "code"];

interface FormState {
  kind: ArtifactKind;
  name: string;
  version: string;
  description: string;
  immutable: boolean;
}

function formFromArtifact(artifact: {
  name: string;
  version: string | null;
  description: string;
  immutable: boolean;
}): FormState {
  return {
    kind: "dataset", // unused in edit mode; kind is immutable after create
    name: artifact.name,
    version: artifact.version ?? "",
    description: artifact.description,
    immutable: artifact.immutable,
  };
}

export interface ArtifactDialogProps {
  open: boolean;
  onClose: () => void;
  /** Preselected kind for add mode (from the active tab). */
  kind: ArtifactKind;
  /** Artifact record in edit mode; null in add mode. */
  artifact: ArtifactRecord | null;
}

export function ArtifactDialog({ open, onClose, kind, artifact }: ArtifactDialogProps) {
  const t = useT();
  const editing = artifact !== null;

  const [form, setForm] = useState<FormState>({
    kind,
    name: "",
    version: "",
    description: "",
    immutable: kind !== "code",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setForm(
      artifact === null
        ? { kind, name: "", version: "", description: "", immutable: kind !== "code" }
        : formFromArtifact(artifact),
    );
    setSaving(false);
    setError(null);
  }, [open, artifact, kind]);

  const submit = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const store = useWorkspaceStore.getState();
      if (artifact === null) {
        const body: ArtifactCreate = {
          kind: form.kind,
          name: form.name.trim(),
          version: form.version.trim() === "" ? null : form.version.trim(),
          description: form.description.trim(),
          immutable: form.immutable,
        };
        await store.createArtifact(body);
      } else {
        await store.patchArtifact(artifact.artifact_id, {
          name: form.name.trim(),
          version: form.version.trim() === "" ? null : form.version.trim(),
          description: form.description.trim(),
          immutable: form.immutable,
        });
      }
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setSaving(false);
    }
  };

  const canSubmit = form.name.trim() !== "" && !saving;

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={
        editing
          ? tf(t.workspace.editArtifactTitle, { name: artifact.name })
          : t.workspace.addArtifactTitle
      }
      width={480}
    >
      <form
        className="ws-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) void submit();
        }}
      >
        {!editing && (
          <Field label={t.workspace.artifactKind} htmlFor="ws-artifact-kind">
            <Select
              id="ws-artifact-kind"
              value={form.kind}
              onChange={(event) => {
                const next = event.target.value as ArtifactKind;
                setForm((c) => ({
                  ...c,
                  kind: next,
                  // Backend default: immutable unless kind is code.
                  immutable: next === "code" ? false : c.immutable,
                }));
              }}
            >
              {KINDS.map((option) => (
                <option key={option} value={option}>
                  {option === "dataset"
                    ? t.workspace.kindDataset
                    : option === "model"
                      ? t.workspace.kindModel
                      : t.workspace.kindCode}
                </option>
              ))}
            </Select>
          </Field>
        )}

        <div className="ws-dialog__pair">
          <Field label={t.workspace.artifactName} htmlFor="ws-artifact-name">
            <TextInput
              id="ws-artifact-name"
              value={form.name}
              onChange={(event) => setForm((c) => ({ ...c, name: event.target.value }))}
            />
          </Field>
          <Field
            label={t.workspace.artifactVersion}
            htmlFor="ws-artifact-version"
            hint={t.workspace.optionalHint}
          >
            <TextInput
              id="ws-artifact-version"
              className="mono"
              placeholder={t.workspace.versionPlaceholder}
              value={form.version}
              onChange={(event) => setForm((c) => ({ ...c, version: event.target.value }))}
            />
          </Field>
        </div>

        <Field label={t.workspace.description} htmlFor="ws-artifact-desc">
          <textarea
            id="ws-artifact-desc"
            className="text-input ws-textarea"
            rows={3}
            value={form.description}
            onChange={(event) => setForm((c) => ({ ...c, description: event.target.value }))}
          />
        </Field>

        <Field label={t.workspace.artifactImmutable} hint={t.workspace.immutableHint}>
          <button
            type="button"
            role="switch"
            aria-checked={form.immutable}
            aria-label={t.workspace.artifactImmutable}
            className={cx("ws-switch", form.immutable && "ws-switch--on")}
            onClick={() => setForm((c) => ({ ...c, immutable: !c.immutable }))}
          >
            <span className="ws-switch__thumb" />
          </button>
        </Field>

        {error !== null && <p className="field__error">{error}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={saving}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmit}>
            {saving ? t.dialog.saving : editing ? t.dialog.save : t.workspace.addArtifact}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

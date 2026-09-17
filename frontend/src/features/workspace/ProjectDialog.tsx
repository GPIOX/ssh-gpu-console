/**
 * Add/Edit project dialog. Artifacts attach via checkboxes fed from the
 * workspace store (one dataset/model serves many projects — references, not
 * copies). Edit mode PATCHes the same four fields.
 */

import { useEffect, useState } from "react";
import { Button, Chip, Dialog, Field, TextInput } from "../../design";
import { tf, useT } from "../../i18n";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ArtifactRecord, ProjectRecord } from "../../types/workspace";
import { artifactLabel, kindLabel, splitList } from "./shared";
import "./workspace.css";

interface FormState {
  name: string;
  description: string;
  tags: string;
  artifactIds: string[];
}

function formFromProject(project: ProjectRecord): FormState {
  return {
    name: project.name,
    description: project.description,
    tags: project.tags.join(", "),
    artifactIds: [...project.artifact_ids],
  };
}

const EMPTY_FORM: FormState = { name: "", description: "", tags: "", artifactIds: [] };

export interface ProjectDialogProps {
  open: boolean;
  onClose: () => void;
  /** Project record in edit mode; null in add mode. */
  project: ProjectRecord | null;
}

export function ProjectDialog({ open, onClose, project }: ProjectDialogProps) {
  const t = useT();
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setForm(project === null ? EMPTY_FORM : formFromProject(project));
    setSaving(false);
    setError(null);
  }, [open, project]);

  const toggleArtifact = (artifactId: string) => {
    setForm((current) => ({
      ...current,
      artifactIds: current.artifactIds.includes(artifactId)
        ? current.artifactIds.filter((id) => id !== artifactId)
        : [...current.artifactIds, artifactId],
    }));
  };

  const submit = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    const body = {
      name: form.name.trim(),
      description: form.description.trim(),
      artifact_ids: form.artifactIds,
      tags: splitList(form.tags),
    };
    try {
      const store = useWorkspaceStore.getState();
      if (project === null) {
        await store.createProject(body);
      } else {
        await store.patchProject(project.project_id, body);
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
        project === null
          ? t.workspace.addProjectTitle
          : tf(t.workspace.editProjectTitle, { name: project.name })
      }
      width={520}
    >
      <form
        className="ws-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) void submit();
        }}
      >
        <Field label={t.workspace.projectName} htmlFor="ws-project-name">
          <TextInput
            id="ws-project-name"
            value={form.name}
            placeholder={t.workspace.namePlaceholder}
            onChange={(event) => setForm((c) => ({ ...c, name: event.target.value }))}
          />
        </Field>

        <Field label={t.workspace.description} htmlFor="ws-project-desc">
          <textarea
            id="ws-project-desc"
            className="text-input ws-textarea"
            rows={3}
            value={form.description}
            onChange={(event) => setForm((c) => ({ ...c, description: event.target.value }))}
          />
        </Field>

        <Field label={t.workspace.tags} htmlFor="ws-project-tags" hint={t.workspace.tagsCommaHint}>
          <TextInput
            id="ws-project-tags"
            value={form.tags}
            onChange={(event) => setForm((c) => ({ ...c, tags: event.target.value }))}
          />
        </Field>

        <Field label={t.workspace.artifacts}>
          {artifacts.length === 0 ? (
            <p className="ws-hint">{t.workspace.noArtifactsYet}</p>
          ) : (
            <div className="ws-checklist">
              {artifacts.map((artifact) => (
                <ArtifactCheckbox
                  key={artifact.artifact_id}
                  artifact={artifact}
                  checked={form.artifactIds.includes(artifact.artifact_id)}
                  onToggle={() => toggleArtifact(artifact.artifact_id)}
                />
              ))}
            </div>
          )}
        </Field>

        {error !== null && <p className="field__error">{error}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={saving}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmit}>
            {saving ? t.dialog.saving : project === null ? t.workspace.addProject : t.dialog.save}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function ArtifactCheckbox({
  artifact,
  checked,
  onToggle,
}: {
  artifact: ArtifactRecord;
  checked: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  return (
    <label className="ws-check">
      <input type="checkbox" checked={checked} onChange={onToggle} aria-label={artifactLabel(artifact)} />
      <span className="ws-check__name mono">{artifactLabel(artifact)}</span>
      <Chip>{kindLabel(t, artifact.kind)}</Chip>
    </label>
  );
}

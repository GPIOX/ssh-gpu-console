/**
 * Edit a project's default transfer exclusion patterns. One mono text input,
 * comma separated (splitList semantics shared with tags); PATCHes only the
 * transfer_excludes field. These patterns decide what a transfer of any of
 * the project's artifacts will NOT copy — rsync gets them as --exclude=arg,
 * the relay matches entry names during the walk.
 */

import { useEffect, useState } from "react";
import { Button, Dialog, Field, TextInput } from "../../design";
import { useT } from "../../i18n";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ProjectRecord } from "../../types/workspace";
import { splitList } from "./shared";

export interface ProjectExcludesDialogProps {
  open: boolean;
  onClose: () => void;
  project: ProjectRecord | null;
}

export function ProjectExcludesDialog({ open, onClose, project }: ProjectExcludesDialogProps) {
  const t = useT();
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setText(project?.transfer_excludes.join(", ") ?? "");
    setSaving(false);
    setError(null);
  }, [open, project]);

  const submit = async (): Promise<void> => {
    if (project === null) return;
    setSaving(true);
    setError(null);
    try {
      const store = useWorkspaceStore.getState();
      await store.patchProject(project.project_id, {
        transfer_excludes: splitList(text),
      });
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} title={t.workspace.editSyncExcludes} width={480}>
      <form
        className="ws-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (!saving) void submit();
        }}
      >
        <Field
          label={t.workspace.syncExcludes}
          htmlFor="ws-project-excludes"
          hint={t.workspace.syncExcludesHint}
        >
          <TextInput
            id="ws-project-excludes"
            className="mono"
            spellCheck={false}
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
        </Field>

        {error !== null && <p className="field__error">{error}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={saving}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={saving}>
            {saving ? t.dialog.saving : t.dialog.save}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

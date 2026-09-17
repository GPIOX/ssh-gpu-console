/**
 * Add/Edit launch-config dialog. Launch configs are STRUCTURED data: program
 * text + comma-separated args — never a shell string (the backend rejects
 * shell metacharacters in program/working_dir with 422, mirrored here as an
 * inline error). env_vars are simple name/value rows; required artifacts
 * attach via checkboxes. `project` pins the owner (ProjectDetail); null shows
 * a project select (launches tab).
 */

import { useEffect, useState } from "react";
import { Button, Chip, Dialog, Field, Select, TextInput } from "../../design";
import { tf, useT } from "../../i18n";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type {
  LaunchConfigCreate,
  LaunchConfigPatch,
  LaunchConfigRecord,
  ProjectRecord,
} from "../../types/workspace";
import { artifactLabel, gibToBytes, hasShellMeta, kindLabel, splitList } from "./shared";
import "./workspace.css";

interface EnvRow {
  name: string;
  value: string;
}

interface FormState {
  projectId: string;
  name: string;
  workingDir: string;
  program: string;
  args: string;
  environment: string;
  envVars: EnvRow[];
  requiredIds: string[];
  gpuCount: string;
  minVramGib: string;
}

function envRowsFrom(record: Record<string, string>): EnvRow[] {
  return Object.entries(record).map(([name, value]) => ({ name, value }));
}

function formFromConfig(config: LaunchConfigRecord, projects: ProjectRecord[]): FormState {
  return {
    projectId: projects.some((p) => p.project_id === config.project_id)
      ? config.project_id
      : "",
    name: config.name,
    workingDir: config.working_dir ?? "",
    program: config.program,
    args: config.args.join(", "),
    environment: config.environment ?? "",
    envVars: envRowsFrom(config.env_vars),
    requiredIds: [...config.required_artifact_ids],
    gpuCount: config.gpu_count !== null ? String(config.gpu_count) : "",
    minVramGib:
      config.min_vram_b !== null && config.min_vram_b % 1024 ** 3 === 0
        ? String(config.min_vram_b / 1024 ** 3)
        : "",
  };
}

const EMPTY_FORM: FormState = {
  projectId: "",
  name: "",
  workingDir: "",
  program: "",
  args: "",
  environment: "",
  envVars: [],
  requiredIds: [],
  gpuCount: "",
  minVramGib: "",
};

function parsePositiveInt(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return "invalid";
  return Number.parseInt(trimmed, 10);
}

/** Empty rows drop out; a row only survives when its name is non-empty. */
function envRecordFrom(rows: EnvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    if (row.name.trim() !== "") out[row.name.trim()] = row.value;
  }
  return out;
}

export interface LaunchConfigDialogProps {
  open: boolean;
  onClose: () => void;
  /** Edit mode record; null in add mode. */
  config: LaunchConfigRecord | null;
  /** All projects (for the select in add mode and name resolution). */
  projects: ProjectRecord[];
  /** Pin the owning project (ProjectDetail context); null shows a select. */
  project: ProjectRecord | null;
}

export function LaunchConfigDialog({
  open,
  onClose,
  config,
  projects,
  project,
}: LaunchConfigDialogProps) {
  const t = useT();
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const pinned = project !== null;
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setForm(
      config === null
        ? { ...EMPTY_FORM, projectId: project?.project_id ?? "" }
        : formFromConfig(config, projects),
    );
    setSaving(false);
    setError(null);
  }, [open, config, project, projects]);

  const setField = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((current) => ({ ...current, [key]: value }));

  const setEnvVar = (index: number, patch: Partial<EnvRow>) =>
    setField(
      "envVars",
      form.envVars.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    );

  const gpu = parsePositiveInt(form.gpuCount);
  const minVramEmpty = form.minVramGib.trim() === "";
  const minVramBytes = gibToBytes(form.minVramGib);
  const minVramInvalid = !minVramEmpty && minVramBytes === null;
  const programValid = form.program.trim() !== "" && !hasShellMeta(form.program.trim());
  const workingDirOk = form.workingDir.trim() === "" || !hasShellMeta(form.workingDir.trim());
  const projectChosen = pinned ? true : form.projectId.trim() !== "";

  const buildBody = () => ({
    project_id: pinned ? project.project_id : form.projectId.trim(),
    name: form.name.trim(),
    working_dir: form.workingDir.trim() === "" ? null : form.workingDir.trim(),
    program: form.program.trim(),
    args: splitList(form.args),
    environment: form.environment.trim() === "" ? null : form.environment.trim(),
    env_vars: envRecordFrom(form.envVars),
    required_artifact_ids: form.requiredIds,
    gpu_count: typeof gpu === "number" ? gpu : null,
    min_vram_b: minVramBytes,
  });

  const submit = async (): Promise<void> => {
    setSaving(true);
    setError(null);
    try {
      const store = useWorkspaceStore.getState();
      if (config === null) {
        await store.createLaunchConfig(buildBody() satisfies LaunchConfigCreate);
      } else {
        const body = buildBody();
        const patch: LaunchConfigPatch = {
          name: body.name,
          working_dir: body.working_dir,
          program: body.program,
          args: body.args,
          environment: body.environment,
          env_vars: body.env_vars,
          required_artifact_ids: body.required_artifact_ids,
          gpu_count: body.gpu_count,
          min_vram_b: body.min_vram_b,
        };
        await store.patchLaunchConfig(config.launch_config_id, patch);
      }
      onClose();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setSaving(false);
    }
  };

  const canSubmit =
    form.name.trim() !== "" && programValid && workingDirOk && projectChosen && !saving;

  const title =
    config === null
      ? t.workspace.addLaunchConfig
      : tf(t.workspace.editLaunchConfig, { name: config.name });

  return (
    <Dialog open={open} onClose={onClose} title={title} width={540}>
      <form
        className="ws-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) void submit();
        }}
      >
        {!pinned && (
          <Field label={t.workspace.lcProject} htmlFor="ws-lc-project">
            <Select
              id="ws-lc-project"
              value={form.projectId}
              onChange={(event) => setField("projectId", event.target.value)}
            >
              <option value="">—</option>
              {projects.map((option) => (
                <option key={option.project_id} value={option.project_id}>
                  {option.name}
                </option>
              ))}
            </Select>
          </Field>
        )}

        <div className="ws-dialog__pair">
          <Field label={t.workspace.lcName} htmlFor="ws-lc-name">
            <TextInput
              id="ws-lc-name"
              value={form.name}
              onChange={(event) => setField("name", event.target.value)}
            />
          </Field>
          <Field
            label={t.workspace.lcWorkingDir}
            htmlFor="ws-lc-workdir"
            hint={t.workspace.optionalHint}
            error={workingDirOk ? null : t.workspace.programInvalid}
          >
            <TextInput
              id="ws-lc-workdir"
              className="mono"
              value={form.workingDir}
              onChange={(event) => setField("workingDir", event.target.value)}
            />
          </Field>
        </div>

        <div className="ws-dialog__pair ws-dialog__pair--uneven">
          <Field
            label={t.workspace.lcProgram}
            htmlFor="ws-lc-program"
            error={programValid ? null : t.workspace.programInvalid}
          >
            <TextInput
              id="ws-lc-program"
              className="mono"
              placeholder={t.workspace.programPlaceholder}
              value={form.program}
              onChange={(event) => setField("program", event.target.value)}
            />
          </Field>
          <Field label={t.workspace.lcArgs} htmlFor="ws-lc-args" hint={t.workspace.argsCommaHint}>
            <textarea
              id="ws-lc-args"
              className="text-input ws-textarea"
              rows={2}
              value={form.args}
              onChange={(event) => setField("args", event.target.value)}
            />
          </Field>
        </div>

        <Field
          label={t.workspace.lcEnvironment}
          htmlFor="ws-lc-env"
          hint={t.workspace.optionalHint}
        >
          <TextInput
            id="ws-lc-env"
            value={form.environment}
            onChange={(event) => setField("environment", event.target.value)}
          />
        </Field>

        <Field label={t.workspace.lcEnvVars}>
          <div className="ws-envrows">
            {form.envVars.map((row, index) => (
              <div key={index} className="ws-envrow">
                <TextInput
                  aria-label={`${t.workspace.envVarName} ${index + 1}`}
                  placeholder={t.workspace.envVarName}
                  className="mono"
                  value={row.name}
                  onChange={(event) => setEnvVar(index, { name: event.target.value })}
                />
                <TextInput
                  aria-label={`${t.workspace.envVarValue} ${index + 1}`}
                  placeholder={t.workspace.envVarValue}
                  className="mono"
                  value={row.value}
                  onChange={(event) => setEnvVar(index, { value: event.target.value })}
                />
                <Button
                  onClick={() =>
                    setField(
                      "envVars",
                      form.envVars.filter((_, i) => i !== index),
                    )
                  }
                  disabled={saving}
                >
                  {t.workspace.removeEnvVar}
                </Button>
              </div>
            ))}
            <Button
              onClick={() => setField("envVars", [...form.envVars, { name: "", value: "" }])}
              disabled={saving}
            >
              {t.workspace.addEnvVar}
            </Button>
          </div>
        </Field>

        <Field label={t.workspace.lcRequiredArtifacts}>
          {artifacts.length === 0 ? (
            <p className="ws-hint">{t.workspace.noArtifactsYet}</p>
          ) : (
            <div className="ws-checklist">
              {artifacts.map((item) => (
                <label key={item.artifact_id} className="ws-check">
                  <input
                    type="checkbox"
                    checked={form.requiredIds.includes(item.artifact_id)}
                    aria-label={artifactLabel(item)}
                    onChange={() => {
                      const id = item.artifact_id;
                      setField(
                        "requiredIds",
                        form.requiredIds.includes(id)
                          ? form.requiredIds.filter((x) => x !== id)
                          : [...form.requiredIds, id],
                      );
                    }}
                  />
                  <span className="ws-check__name mono">{artifactLabel(item)}</span>
                  <Chip>{kindLabel(t, item.kind)}</Chip>
                </label>
              ))}
            </div>
          )}
        </Field>

        <div className="ws-dialog__pair">
          <Field
            label={t.workspace.lcGpuCount}
            htmlFor="ws-lc-gpus"
            hint={t.workspace.optionalHint}
            error={gpu === "invalid" ? t.workspace.gpuCountInvalid : null}
          >
            <TextInput
              id="ws-lc-gpus"
              type="number"
              min={1}
              max={64}
              value={form.gpuCount}
              onChange={(event) => setField("gpuCount", event.target.value)}
            />
          </Field>
          <Field
            label={t.workspace.lcMinVram}
            htmlFor="ws-lc-vram"
            hint={t.workspace.optionalHint}
            error={minVramInvalid ? t.workspace.minVramInvalid : null}
          >
            <TextInput
              id="ws-lc-vram"
              type="number"
              min={0}
              step={1}
              value={form.minVramGib}
              onChange={(event) => setField("minVramGib", event.target.value)}
            />
          </Field>
        </div>

        {error !== null && <p className="field__error">{error}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={onClose} disabled={saving}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={!canSubmit}>
            {saving
              ? t.dialog.saving
              : config === null
                ? t.workspace.addLaunchConfig
                : t.dialog.save}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

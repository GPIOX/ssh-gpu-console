/**
 * Workspace page: a quiet tab strip (项目 / 数据集 / 模型 / 启动配置 — the
 * page title already lives in the context header), the active tab content,
 * and a quiet per-server roots section. Loads the whole catalog once on
 * open; refetch is manual (per-tab refresh buttons) — Phase 1 is REST only,
 * no polling.
 */

import { useEffect, useState } from "react";
import { Button, Field, Panel, Skeleton, TextInput } from "../../design";
import { useConsoleStore } from "../../store/consoleStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ServerRecord } from "../../types/models";
import type { ServerRootsUpdate } from "../../types/workspace";
import { useT } from "../../i18n";
import { WORKSPACE_SECTIONS, routeToHash, type WorkspaceSection } from "../../shell/routes";
import { cx } from "../../utils/cx";
import { ProjectsTab } from "./ProjectsTab";
import { ProjectDetail } from "./ProjectDetail";
import { ArtifactsTab } from "./ArtifactsTab";
import { LaunchConfigsTab } from "./LaunchConfigsTab";
import { rootPathValid } from "./shared";
import "./workspace.css";

const SECTION_LABELS: Record<WorkspaceSection, keyof ReturnType<typeof useT>["workspace"]> = {
  projects: "tabProjects",
  datasets: "tabDatasets",
  models: "tabModels",
  launches: "tabLaunches",
};

const ROOT_FIELDS = [
  { key: "project_root", labelKey: "rootProject" },
  { key: "dataset_root", labelKey: "rootDataset" },
  { key: "model_root", labelKey: "rootModel" },
  { key: "output_root", labelKey: "rootOutput" },
] as const;

export function WorkspacePage() {
  const t = useT();
  const route = useConsoleStore((state) => state.route);
  const navigate = useConsoleStore((state) => state.navigate);
  const loadWorkspace = useWorkspaceStore((state) => state.loadWorkspace);

  useEffect(() => {
    void loadWorkspace(); // one catalog fetch per open; never polled
  }, [loadWorkspace]);

  const section = route.name === "workspace" ? route.section : "projects";
  const projectId = route.name === "workspace" ? route.projectId : undefined;

  return (
    <div className="ws-page">
      <nav className="tabs ws-tabs" aria-label={t.workspace.title}>
        {WORKSPACE_SECTIONS.map((key) => (
          <button
            key={key}
            type="button"
            className={cx("tabs__tab", section === key && "tabs__tab--active")}
            onClick={() => navigate(routeToHash({ name: "workspace", section: key }))}
          >
            {t.workspace[SECTION_LABELS[key]]}
          </button>
        ))}
      </nav>

      {section === "projects" &&
        (projectId === undefined ? <ProjectsTab /> : <ProjectDetail projectId={projectId} />)}
      {section === "datasets" && <ArtifactsTab kind="dataset" />}
      {section === "models" && <ArtifactsTab kind="model" />}
      {section === "launches" && <LaunchConfigsTab />}

      <ServerRootsSection />
    </div>
  );
}

/** Per-server editable roots, saved with PUT (whole-record replace; empty → null). */
function ServerRootsSection() {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  return (
    <section className="ws-roots">
      <header className="ws-roots__head">
        <h2 className="ws-roots__title micro-label">{t.workspace.serverRoots}</h2>
        <p className="ws-roots__hint">{t.workspace.rootsHint}</p>
      </header>
      {servers.length === 0 ? (
        <Panel>
          <p className="ws-hint">{t.workspace.noServersForRoots}</p>
        </Panel>
      ) : (
        servers.map((server) => <ServerRootsEditor key={server.server_id} server={server} />)
      )}
    </section>
  );
}

function ServerRootsEditor({ server }: { server: ServerRecord }) {
  const t = useT();
  const roots = useWorkspaceStore((state) => state.serverRoots[server.server_id]);
  const loading = useWorkspaceStore((state) => state.rootsLoading[server.server_id]);
  const loadError = useWorkspaceStore((state) => state.rootsErrors[server.server_id] ?? "");
  const loadServerRoots = useWorkspaceStore((state) => state.loadServerRoots);
  const saveServerRoots = useWorkspaceStore((state) => state.saveServerRoots);

  const [draft, setDraft] = useState<Record<(typeof ROOT_FIELDS)[number]["key"], string>>({
    project_root: "",
    dataset_root: "",
    model_root: "",
    output_root: "",
  });
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void loadServerRoots(server.server_id);
  }, [loadServerRoots, server.server_id]);

  // Seed the form when the cache arrives — but never clobber user edits.
  useEffect(() => {
    if (dirty || roots === undefined) return;
    setDraft({
      project_root: roots.project_root ?? "",
      dataset_root: roots.dataset_root ?? "",
      model_root: roots.model_root ?? "",
      output_root: roots.output_root ?? "",
    });
  }, [dirty, roots]);

  const fields = ROOT_FIELDS.map(({ key, labelKey }) => ({
    key,
    label: t.workspace[labelKey],
    value: draft[key],
    valid: draft[key] === "" || rootPathValid(draft[key]),
  }));
  const allValid = fields.every((field) => field.valid);

  const save = async (): Promise<void> => {
    setSaving(true);
    setSaveError(null);
    const update: ServerRootsUpdate = {
      project_root: draft.project_root.trim() === "" ? null : draft.project_root.trim(),
      dataset_root: draft.dataset_root.trim() === "" ? null : draft.dataset_root.trim(),
      model_root: draft.model_root.trim() === "" ? null : draft.model_root.trim(),
      output_root: draft.output_root.trim() === "" ? null : draft.output_root.trim(),
    };
    try {
      await saveServerRoots(server.server_id, update);
      setDirty(false);
      setSaved(true);
    } catch (cause) {
      setSaveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Panel className="ws-roots__panel">
      <div className="ws-roots__server">
        <span className="ws-roots__server-name">{server.display_name}</span>
        <span className="ws-roots__endpoint mono">{server.ssh_host}</span>
        <div className="ws-roots__spacer" />
        {saved && !dirty && saveError === null && (
          <span className="ws-roots__saved">{t.workspace.rootsSaved}</span>
        )}
        <Button onClick={() => void save()} disabled={!allValid || saving || !dirty}>
          {saving ? t.workspace.savingRoots : t.workspace.saveRoots}
        </Button>
      </div>
      {loadError !== "" ? (
        <p className="field__error">{loadError}</p>
      ) : loading === true || roots === undefined ? (
        <div className="ws-roots__grid">
          {[0, 1, 2, 3].map((row) => (
            <Skeleton key={row} height={30} radius="7px" />
          ))}
        </div>
      ) : (
        <div className="ws-roots__grid">
          {fields.map((field) => (
            <Field
              key={field.key}
              label={field.label}
              htmlFor={`ws-root-${server.server_id}-${field.key}`}
              error={field.valid ? null : t.workspace.rootInvalid}
            >
              <TextInput
                id={`ws-root-${server.server_id}-${field.key}`}
                className="mono"
                spellCheck={false}
                value={field.value}
                onChange={(event) => {
                  setDirty(true);
                  setSaved(false);
                  setDraft((current) => ({ ...current, [field.key]: event.target.value }));
                }}
              />
            </Field>
          ))}
        </div>
      )}
      {saveError !== null && <p className="field__error">{saveError}</p>}
    </Panel>
  );
}

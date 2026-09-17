/**
 * Projects tab: one panel per project (name, description, artifact/launch
 * counts, tag chips), a per-row ⋯ menu (Edit / Remove), and a named
 * destructive remove dialog. Row click opens the project detail route.
 */

import { useState } from "react";
import { Button, Chip, Dialog, EmptyState, ErrorPanel, Menu, Panel, Skeleton } from "../../design";
import { useConsoleStore } from "../../store/consoleStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { ProjectRecord } from "../../types/workspace";
import { useRelative, useT, tf } from "../../i18n";
import { useNow } from "../../utils/clock";
import { ProjectDialog } from "./ProjectDialog";
import "./workspace.css";

type DialogTarget = { mode: "add" } | { mode: "edit"; project: ProjectRecord } | null;

export function ProjectsTab() {
  const t = useT();
  const relative = useRelative();
  const now = useNow();
  const navigate = useConsoleStore((state) => state.navigate);
  const projects = useWorkspaceStore((state) => state.projects);
  const loading = useWorkspaceStore((state) => state.projectsLoading);
  const error = useWorkspaceStore((state) => state.projectsError);
  const loadProjects = useWorkspaceStore((state) => state.loadProjects);
  const deleteProject = useWorkspaceStore((state) => state.deleteProject);

  const [dialog, setDialog] = useState<DialogTarget>(null);
  const [removeTarget, setRemoveTarget] = useState<ProjectRecord | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const remove = async (): Promise<void> => {
    if (removeTarget === null) return;
    setRemoving(true);
    setRemoveError(null);
    try {
      await deleteProject(removeTarget.project_id);
      setRemoveTarget(null);
    } catch (cause) {
      setRemoveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setRemoving(false);
    }
  };

  const add = (
    <Button variant="primary" onClick={() => setDialog({ mode: "add" })}>
      {t.workspace.addProject}
    </Button>
  );

  let body;
  if (error !== null) {
    body = (
      <Panel>
        <ErrorPanel
          title={t.workspace.noProjects}
          detail={error}
          onRetry={() => void loadProjects()}
          retryLabel={t.common.retry}
        />
      </Panel>
    );
  } else if (loading && projects.length === 0) {
    body = (
      <Panel>
        <div className="ws-skeleton" aria-label={t.workspace.tabProjects}>
          {[0, 1, 2].map((row) => (
            <div key={row} className="ws-skeleton__row">
              <Skeleton width={160} height={14} radius="5px" />
              <Skeleton width={240} height={11} />
              <Skeleton width={70} height={16} radius="8px" />
            </div>
          ))}
        </div>
      </Panel>
    );
  } else if (projects.length === 0) {
    body = (
      <Panel>
        <EmptyState title={t.workspace.noProjects} hint={t.workspace.noProjectsHint} action={add} />
      </Panel>
    );
  } else {
    body = (
      <div className="ws-list">
        {projects.map((project) => (
          <Panel
            key={project.project_id}
            interactive
            className="ws-row"
            onClick={() => navigate(`#/workspace/projects/${encodeURIComponent(project.project_id)}`)}
          >
            <div className="ws-row__main">
              <span className="ws-row__name">{project.name}</span>
              {project.description !== "" && (
                <span className="ws-row__desc">{project.description}</span>
              )}
              <span className="ws-row__facts mono tnum">
                {tf(t.workspace.artifactCount, { n: project.artifact_ids.length })}
                {" · "}
                {tf(t.workspace.launchConfigCount, { n: project.launch_config_ids.length })}
                {" · "}
                {tf(t.common.updated, { ago: relative(project.updated_at, now) })}
              </span>
            </div>
            <div className="ws-row__tags">
              {project.tags.map((tag) => (
                <Chip key={tag} mono>
                  {tag}
                </Chip>
              ))}
            </div>
            <span className="ws-menu-hold" onClick={(event) => event.stopPropagation()}>
              <Menu
                items={[
                  {
                    id: "edit",
                    label: t.common.edit,
                    onSelect: () => setDialog({ mode: "edit", project }),
                  },
                  {
                    id: "remove",
                    label: t.common.remove,
                    danger: true,
                    onSelect: () => {
                      setRemoveError(null);
                      setRemoveTarget(project);
                    },
                  },
                ]}
                triggerLabel={tf(t.workspace.projectActions, { name: project.name })}
              />
            </span>
          </Panel>
        ))}
      </div>
    );
  }

  return (
    <section className="ws-tab">
      <header className="ws-tab__head">
        <h2 className="ws-tab__title micro-label">{t.workspace.tabProjects}</h2>
        <div className="ws-tab__spacer" />
        <Button onClick={() => void loadProjects()} disabled={loading}>
          {t.workspace.refresh}
        </Button>
        {projects.length > 0 && add}
      </header>
      {body}

      <ProjectDialog
        open={dialog !== null}
        onClose={() => setDialog(null)}
        project={dialog?.mode === "edit" ? dialog.project : null}
      />

      <Dialog
        open={removeTarget !== null}
        onClose={() => setRemoveTarget(null)}
        title={
          removeTarget === null
            ? undefined
            : tf(t.workspace.removeProjectConfirm, { name: removeTarget.name })
        }
      >
        {removeTarget !== null && (
          <>
            <p className="ws-confirm__text">
              {tf(t.workspace.removeProjectTitle, { name: removeTarget.name })}{" "}
              {tf(t.workspace.removeProjectBody, { name: removeTarget.name })}
            </p>
            {removeError !== null && <p className="field__error">{removeError}</p>}
            <div className="ws-dialog__actions">
              <Button onClick={() => setRemoveTarget(null)} disabled={removing}>
                {t.common.cancel}
              </Button>
              <Button variant="primary" disabled={removing} onClick={() => void remove()}>
                {tf(t.workspace.removeProjectConfirm, { name: removeTarget.name })}
              </Button>
            </div>
          </>
        )}
      </Dialog>
    </section>
  );
}

/**
 * Launch-configs tab: every stored launch config across projects — name,
 * mono program+args, GPU chip, owning project. Add opens the shared dialog
 * with a project select; edit/remove live in the row ⋯ menu.
 */

import { useState } from "react";
import { Button, Chip, Dialog, EmptyState, ErrorPanel, Menu, Panel, Skeleton } from "../../design";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { LaunchConfigRecord } from "../../types/workspace";
import { useT, tf } from "../../i18n";
import { LaunchConfigDialog } from "./LaunchConfigDialog";
import "./workspace.css";

function commandLine(config: LaunchConfigRecord): string {
  const args = config.args.length > 0 ? ` ${config.args.join(" ")}` : "";
  return `${config.program}${args}`;
}

export function LaunchConfigsTab() {
  const t = useT();
  const configs = useWorkspaceStore((state) => state.launchConfigs);
  const projects = useWorkspaceStore((state) => state.projects);
  const loading = useWorkspaceStore((state) => state.launchConfigsLoading);
  const error = useWorkspaceStore((state) => state.launchConfigsError);
  const loadLaunchConfigs = useWorkspaceStore((state) => state.loadLaunchConfigs);
  const deleteLaunchConfig = useWorkspaceStore((state) => state.deleteLaunchConfig);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<LaunchConfigRecord | null>(null);
  const [removeTarget, setRemoveTarget] = useState<LaunchConfigRecord | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const projectName = (projectId: string): string =>
    projects.find((p) => p.project_id === projectId)?.name ?? t.workspace.launchProjectMissing;

  const remove = async (): Promise<void> => {
    if (removeTarget === null) return;
    setRemoving(true);
    setRemoveError(null);
    try {
      await deleteLaunchConfig(removeTarget.launch_config_id);
      setRemoveTarget(null);
    } catch (cause) {
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
      {t.workspace.addLaunchConfig}
    </Button>
  );

  let body;
  if (error !== null) {
    body = (
      <Panel>
        <ErrorPanel
          title={t.workspace.noLaunches}
          detail={error}
          onRetry={() => void loadLaunchConfigs()}
          retryLabel={t.common.retry}
        />
      </Panel>
    );
  } else if (loading && configs.length === 0) {
    body = (
      <Panel>
        <div className="ws-skeleton" aria-label={t.workspace.launchConfigs}>
          {[0, 1, 2].map((row) => (
            <div key={row} className="ws-skeleton__row">
              <Skeleton width={150} height={13} radius="5px" />
              <Skeleton width={220} height={11} />
              <Skeleton width={60} height={16} radius="8px" />
            </div>
          ))}
        </div>
      </Panel>
    );
  } else if (configs.length === 0) {
    body = (
      <Panel>
        <EmptyState title={t.workspace.noLaunches} hint={t.workspace.noLaunchesHint} action={add} />
      </Panel>
    );
  } else {
    body = (
      <div className="ws-list">
        {configs.map((config) => (
          <Panel key={config.launch_config_id} className="ws-row">
            <div className="ws-row__main">
              <span className="ws-row__name">{config.name}</span>
              <span className="ws-row__desc mono">{commandLine(config)}</span>
              <span className="ws-row__facts mono">
                {tf(t.workspace.launchProject, { name: projectName(config.project_id) })}
              </span>
            </div>
            <div className="ws-row__tags">
              {config.gpu_count !== null && (
                <Chip mono title={t.workspace.lcGpuCount}>
                  {tf(t.workspace.gpuCountValue, { n: config.gpu_count })}
                </Chip>
              )}
            </div>
            <Menu
              items={[
                {
                  id: "edit",
                  label: t.common.edit,
                  onSelect: () => {
                    setEditTarget(config);
                    setDialogOpen(true);
                  },
                },
                {
                  id: "remove",
                  label: t.common.remove,
                  danger: true,
                  onSelect: () => {
                    setRemoveError(null);
                    setRemoveTarget(config);
                  },
                },
              ]}
              triggerLabel={tf(t.workspace.launchConfigActions, { name: config.name })}
            />
          </Panel>
        ))}
      </div>
    );
  }

  return (
    <section className="ws-tab">
      <header className="ws-tab__head">
        <h2 className="ws-tab__title micro-label">{t.workspace.tabLaunches}</h2>
        <div className="ws-tab__spacer" />
        <Button onClick={() => void loadLaunchConfigs()} disabled={loading}>
          {t.workspace.refresh}
        </Button>
        {projects.length > 0 && configs.length > 0 && add}
      </header>
      {body}

      <LaunchConfigDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        config={editTarget}
        projects={projects}
        project={null}
      />

      <Dialog
        open={removeTarget !== null}
        onClose={() => setRemoveTarget(null)}
        title={
          removeTarget === null
            ? undefined
            : tf(t.workspace.removeLaunchConfigConfirm, { name: removeTarget.name })
        }
      >
        {removeTarget !== null && (
          <>
            <p className="ws-confirm__text">
              {tf(t.workspace.removeLaunchConfigTitle, { name: removeTarget.name })}{" "}
              {t.workspace.removeLaunchConfigBody}
            </p>
            {removeError !== null && <p className="field__error">{removeError}</p>}
            <div className="ws-dialog__actions">
              <Button onClick={() => setRemoveTarget(null)} disabled={removing}>
                {t.common.cancel}
              </Button>
              <Button variant="primary" disabled={removing} onClick={() => void remove()}>
                {tf(t.workspace.removeLaunchConfigConfirm, { name: removeTarget.name })}
              </Button>
            </div>
          </>
        )}
      </Dialog>
    </section>
  );
}

/**
 * Project detail: header (name, description, updated), the distribution
 * matrix (referenced artifacts × registry servers; ✓ present / — missing,
 * read-only from placement declarations), the placements table with explicit
 * inspection, and the project's launch configs (structured fields only).
 */

import { useState } from "react";
import { ArrowLeft } from "@phosphor-icons/react";
import {
  Button,
  Chip,
  Dialog,
  EmptyState,
  IconButton,
  Menu,
  Panel,
  Section,
  Skeleton,
} from "../../design";
import { useConsoleStore } from "../../store/consoleStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import { openNewTransfer } from "../../store/transferStore";
import type {
  ArtifactRecord,
  LaunchConfigRecord,
  PlacementRecord,
} from "../../types/workspace";
import { useRelative, useT, tf } from "../../i18n";
import { useNow } from "../../utils/clock";
import { WORKSPACE_HASH } from "../../shell/routes";
import { artifactLabel, kindLabel } from "./shared";
import { ProjectDialog } from "./ProjectDialog";
import { PlacementDialog } from "./PlacementDialog";
import { PlacementRemoveDialog, PlacementTable } from "./PlacementTable";
import { LaunchConfigDialog } from "./LaunchConfigDialog";
import "./workspace.css";

function commandLine(config: LaunchConfigRecord): string {
  const args = config.args.length > 0 ? ` ${config.args.join(" ")}` : "";
  return `${config.program}${args}`;
}

export function ProjectDetail({ projectId }: { projectId: string }) {
  const t = useT();
  const relative = useRelative();
  const now = useNow();
  const navigate = useConsoleStore((state) => state.navigate);
  const servers = useConsoleStore((state) => state.servers);

  const projects = useWorkspaceStore((state) => state.projects);
  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const placements = useWorkspaceStore((state) => state.placements);
  const launchConfigs = useWorkspaceStore((state) => state.launchConfigs);
  const loading = useWorkspaceStore((state) => state.projectsLoading);
  const deleteProject = useWorkspaceStore((state) => state.deleteProject);
  const deleteLaunchConfig = useWorkspaceStore((state) => state.deleteLaunchConfig);

  const [editOpen, setEditOpen] = useState(false);
  const [removeOpen, setRemoveOpen] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  const [placementDialog, setPlacementDialog] = useState<
    { mode: "add"; artifactId?: string } | { mode: "edit"; placement: PlacementRecord } | null
  >(null);
  const [placementRemove, setPlacementRemove] = useState<PlacementRecord | null>(null);

  const [configDialogOpen, setConfigDialogOpen] = useState(false);
  const [configEditTarget, setConfigEditTarget] = useState<LaunchConfigRecord | null>(null);
  const [configRemoveTarget, setConfigRemoveTarget] = useState<LaunchConfigRecord | null>(null);
  const [configRemoving, setConfigRemoving] = useState(false);
  const [configRemoveError, setConfigRemoveError] = useState<string | null>(null);

  const backToProjects = () => navigate(WORKSPACE_HASH);
  const project = projects.find((p) => p.project_id === projectId);

  if (project === undefined) {
    if (loading) {
      return (
        <div className="ws-detail">
          <BackLink />
          <Panel>
            <div className="ws-skeleton">
              <div className="ws-skeleton__row">
                <Skeleton width={200} height={16} radius="5px" />
                <Skeleton width={300} height={11} />
              </div>
              {[0, 1, 2, 3].map((row) => (
                <div key={row} className="ws-skeleton__row">
                  <Skeleton width={160} height={12} />
                  <Skeleton width={64} height={16} radius="8px" />
                  <Skeleton width={64} height={16} radius="8px" />
                </div>
              ))}
            </div>
          </Panel>
        </div>
      );
    }
    return (
      <div className="ws-detail">
        <BackLink />
        <Panel>
          <EmptyState
            title={t.workspace.projectMissing}
            hint={t.workspace.noProjectsHint}
            action={<Button onClick={backToProjects}>{t.workspace.backToProjects}</Button>}
          />
        </Panel>
      </div>
    );
  }

  const referenced = project.artifact_ids
    .map((id) => artifacts.find((a) => a.artifact_id === id))
    .filter((a): a is ArtifactRecord => a !== undefined);
  const configs = launchConfigs.filter((c) => c.project_id === project.project_id);
  const referencedPlacements = placements.filter((p) =>
    project.artifact_ids.includes(p.artifact_id),
  );

  const remove = async (): Promise<void> => {
    setRemoving(true);
    setRemoveError(null);
    try {
      await deleteProject(project.project_id);
      navigate(WORKSPACE_HASH);
    } catch (cause) {
      setRemoveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setRemoving(false);
    }
  };

  const removeConfig = async (): Promise<void> => {
    if (configRemoveTarget === null) return;
    setConfigRemoving(true);
    setConfigRemoveError(null);
    try {
      await deleteLaunchConfig(configRemoveTarget.launch_config_id);
      setConfigRemoveTarget(null);
    } catch (cause) {
      setConfigRemoveError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setConfigRemoving(false);
    }
  };

  return (
    <div className="ws-detail">
      <BackLink />

      <Panel className="ws-detail__head">
        <div className="ws-detail__identity">
          <h1 className="ws-detail__name">{project.name}</h1>
          {project.description !== "" && <p className="ws-detail__desc">{project.description}</p>}
          <p className="ws-detail__facts mono tnum">
            {tf(t.workspace.artifactCount, { n: project.artifact_ids.length })} ·{" "}
            {tf(t.workspace.launchConfigCount, { n: configs.length })} ·{" "}
            {tf(t.common.updated, { ago: relative(project.updated_at, now) })}
          </p>
        </div>
        <div className="ws-detail__actions">
          <Button onClick={() => setEditOpen(true)}>{t.common.edit}</Button>
          <Button
            onClick={() => {
              setRemoveError(null);
              setRemoveOpen(true);
            }}
          >
            {t.common.remove}
          </Button>
        </div>
      </Panel>

      <Section title={t.workspace.distribution} meta={t.workspace.distributionHint}>
        <Panel>
          <div className="ws-actions-inline">
            <Button onClick={() => setPlacementDialog({ mode: "add" })}>
              {t.workspace.addPlacement}
            </Button>
          </div>
          {referenced.length === 0 ? (
            <EmptyState title={t.workspace.matrixEmpty} hint={t.workspace.noArtifactsYet} />
          ) : servers.length === 0 ? (
            <EmptyState title={t.workspace.noServersForRoots} />
          ) : (
            <div className="ws-matrix-scroll">
              <table className="ws-matrix">
                <thead>
                  <tr>
                    <th>{t.workspace.matrixArtifact}</th>
                    {servers.map((server) => (
                      <th key={server.server_id} title={server.ssh_host}>
                        {server.display_name}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {referenced.map((artifact) => (
                    <tr key={artifact.artifact_id}>
                      <td className="ws-matrix__artifact">
                        <span className="mono">{artifactLabel(artifact)}</span>
                        <Chip>{kindLabel(t, artifact.kind)}</Chip>
                      </td>
                      {servers.map((server) => {
                        const present = placements.some(
                          (p) =>
                            p.artifact_id === artifact.artifact_id &&
                            p.server_id === server.server_id,
                        );
                        return (
                          <td key={server.server_id} className="ws-matrix__cell">
                            {present ? (
                              <span
                                className="ws-matrix__present mono"
                                aria-label={t.workspace.matrixPresent}
                              >
                                ✓
                              </span>
                            ) : (
                              <span
                                className="ws-matrix__missing"
                                aria-label={t.workspace.matrixMissing}
                              >
                                —
                              </span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </Section>

      <Section title={t.workspace.placements}>
        <PlacementTable
          artifacts={referenced}
          placements={referencedPlacements}
          onEdit={(placement) => setPlacementDialog({ mode: "edit", placement })}
          onRemove={(placement) => setPlacementRemove(placement)}
          onSync={(placement) =>
            openNewTransfer({
              artifactId: placement.artifact_id,
              sourcePlacementId: placement.placement_id,
            })
          }
        />
      </Section>

      <Section title={t.workspace.launchConfigs} meta={String(configs.length)}>
        {configs.length === 0 ? (
          <Panel>
            <EmptyState
              title={t.workspace.noLaunchConfigs}
              hint={t.workspace.noLaunchesHint}
              action={
                <Button variant="primary" onClick={() => setConfigDialogOpen(true)}>
                  {t.workspace.addLaunchConfig}
                </Button>
              }
            />
          </Panel>
        ) : (
          <div className="ws-list">
            {configs.map((config) => (
              <Panel key={config.launch_config_id} className="ws-row">
                <div className="ws-row__main">
                  <span className="ws-row__name">{config.name}</span>
                  <span className="ws-row__desc mono">{commandLine(config)}</span>
                  {config.working_dir !== null && (
                    <span className="ws-row__facts mono">{config.working_dir}</span>
                  )}
                </div>
                <div className="ws-row__tags">
                  {config.gpu_count !== null && (
                    <Chip mono>{tf(t.workspace.gpuCountValue, { n: config.gpu_count })}</Chip>
                  )}
                </div>
                <Menu
                  items={[
                    {
                      id: "edit",
                      label: t.common.edit,
                      onSelect: () => {
                        setConfigEditTarget(config);
                        setConfigDialogOpen(true);
                      },
                    },
                    {
                      id: "remove",
                      label: t.common.remove,
                      danger: true,
                      onSelect: () => {
                        setConfigRemoveError(null);
                        setConfigRemoveTarget(config);
                      },
                    },
                  ]}
                  triggerLabel={tf(t.workspace.launchConfigActions, { name: config.name })}
                />
              </Panel>
            ))}
          </div>
        )}
      </Section>

      <ProjectDialog open={editOpen} onClose={() => setEditOpen(false)} project={project} />
      <PlacementDialog
        open={placementDialog !== null}
        onClose={() => setPlacementDialog(null)}
        placement={placementDialog?.mode === "edit" ? placementDialog.placement : null}
        artifacts={referenced}
        artifactId={placementDialog?.mode === "add" ? placementDialog.artifactId : undefined}
      />
      <PlacementRemoveDialog
        placement={placementRemove}
        onClose={() => setPlacementRemove(null)}
      />
      <LaunchConfigDialog
        open={configDialogOpen}
        onClose={() => setConfigDialogOpen(false)}
        config={configEditTarget}
        projects={projects}
        project={project}
      />

      <Dialog
        open={removeOpen}
        onClose={() => setRemoveOpen(false)}
        title={tf(t.workspace.removeProjectConfirm, { name: project.name })}
      >
        <p className="ws-confirm__text">
          {tf(t.workspace.removeProjectTitle, { name: project.name })}{" "}
          {tf(t.workspace.removeProjectBody, { name: project.name })}
        </p>
        {removeError !== null && <p className="field__error">{removeError}</p>}
        <div className="ws-dialog__actions">
          <Button onClick={() => setRemoveOpen(false)} disabled={removing}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" disabled={removing} onClick={() => void remove()}>
            {tf(t.workspace.removeProjectConfirm, { name: project.name })}
          </Button>
        </div>
      </Dialog>

      <Dialog
        open={configRemoveTarget !== null}
        onClose={() => setConfigRemoveTarget(null)}
        title={
          configRemoveTarget === null
            ? undefined
            : tf(t.workspace.removeLaunchConfigConfirm, { name: configRemoveTarget.name })
        }
      >
        {configRemoveTarget !== null && (
          <>
            <p className="ws-confirm__text">
              {tf(t.workspace.removeLaunchConfigTitle, { name: configRemoveTarget.name })}{" "}
              {t.workspace.removeLaunchConfigBody}
            </p>
            {configRemoveError !== null && <p className="field__error">{configRemoveError}</p>}
            <div className="ws-dialog__actions">
              <Button onClick={() => setConfigRemoveTarget(null)} disabled={configRemoving}>
                {t.common.cancel}
              </Button>
              <Button
                variant="primary"
                disabled={configRemoving}
                onClick={() => void removeConfig()}
              >
                {tf(t.workspace.removeLaunchConfigConfirm, { name: configRemoveTarget.name })}
              </Button>
            </div>
          </>
        )}
      </Dialog>
    </div>
  );
}

function BackLink() {
  const navigate = useConsoleStore((state) => state.navigate);
  const t = useT();
  return (
    <div className="ws-detail__back">
      <IconButton
        label={t.workspace.backToProjects}
        onClick={() => navigate(WORKSPACE_HASH)}
      >
        <ArrowLeft size={16} />
      </IconButton>
      <span className="ws-detail__back-label">{t.workspace.backToProjects}</span>
    </div>
  );
}

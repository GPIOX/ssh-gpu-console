/**
 * New transfer dialog. Artifact → source placement (filtered to the artifact)
 * → target server → target path (auto-suggested from the server's kind root
 * + name:version until the user edits it) → method (自动/直接同步/本机中转,
 * default 自动). The target suggestion prefers the artifact's project peers —
 * servers that host a placement of any artifact of a project containing the
 * selected artifact (minus the source): the current choice is kept when it is
 * already a peer, otherwise the first enabled peer wins; without peers only a
 * target that is unset or collides with the source is re-aimed. Every field
 * change debounce-posts /transfers/plan and shows
 * per-method availability + reason — planning is an explicit SSH preflight,
 * so it only runs on deliberate, complete input. The preview also surfaces the
 * preflight's space warning (or, quietly, the target's free space) and the
 * effective project excludes.
 *
 * Phase 4.2D: when the plan says direct rsync is unavailable, the reason area
 * gains a humanized lead line (the raw planner reason stays as secondary mono
 * detail), a 配置直连 button opening DirectAuthSetupDialog for the current
 * (source → target) pair, and — after a change in that dialog — an explicit
 * 重新规划 action that re-runs the plan request.
 */

import { useEffect, useRef, useState } from "react";
import { Button, Chip, Dialog, Field, Select, TextInput } from "../../design";
import { tf, useT } from "../../i18n";
import { useConsoleStore } from "../../store/consoleStore";
import { useTransferStore } from "../../store/transferStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import { transfersApi } from "../../services/transfersApi";
import { formatBytes } from "../../utils/format";
import type {
  TransferPlan,
  TransferRequest,
  TransferStrategy,
  VerifyMode,
} from "../../types/transfers";
import type { ServerRecord } from "../../types/models";
import type {
  ArtifactRecord,
  PlacementRecord,
  ProjectRecord,
} from "../../types/workspace";
import { artifactLabel } from "../workspace/shared";
import { DirectAuthSetupDialog } from "./DirectAuthSetupDialog";
import { cx } from "../../utils/cx";
import "./transfers.css";

const STRATEGY_OPTIONS: TransferStrategy[] = ["auto", "direct_rsync", "local_relay"];
const PLAN_DEBOUNCE_MS = 400;

/** ServerRoots kind → root field used for the target path suggestion. */
function kindRootField(
  kind: ArtifactRecord["kind"],
): "project_root" | "dataset_root" | "model_root" {
  if (kind === "dataset") return "dataset_root";
  if (kind === "model") return "model_root";
  return "project_root";
}

function strategyLabel(t: ReturnType<typeof useT>, strategy: TransferStrategy): string {
  if (strategy === "direct_rsync") return t.transfers.strategyDirect;
  if (strategy === "local_relay") return t.transfers.strategyRelay;
  return t.transfers.strategyAuto;
}

function firstTargetServer(servers: ServerRecord[], exclude: string | undefined): string {
  const enabled = servers.filter((s) => s.enabled && s.server_id !== exclude);
  return enabled[0]?.server_id ?? "";
}

/**
 * Peer deployment context: distinct servers hosting a placement of any
 * artifact that shares a project with the selected artifact (all of the
 * project's artifact_ids count), minus the source server. Order follows the
 * placements list; callers pick by servers-list order.
 */
function peerServerIds(
  artifactId: string,
  projects: ProjectRecord[],
  placements: PlacementRecord[],
  sourceServerId: string | undefined,
): string[] {
  const projectArtifactIds = new Set<string>();
  for (const project of projects) {
    if (project.artifact_ids.includes(artifactId)) {
      for (const id of project.artifact_ids) projectArtifactIds.add(id);
    }
  }
  if (projectArtifactIds.size === 0) return [];
  const peers: string[] = [];
  for (const placement of placements) {
    const serverId = placement.server_id;
    if (
      serverId !== sourceServerId &&
      projectArtifactIds.has(placement.artifact_id) &&
      !peers.includes(serverId)
    ) {
      peers.push(serverId);
    }
  }
  return peers;
}

/**
 * Target suggestion for a (newly selected) artifact. With peer servers, the
 * current target survives only if it is itself a peer; otherwise the first
 * peer that is an enabled server is taken (servers-list order). Without
 * peers, the v1 rule: re-aim only when the target is unset or collides with
 * the source.
 */
function suggestedTargetServer(
  currentTarget: string,
  peers: string[],
  servers: ServerRecord[],
  sourceServerId: string | undefined,
): string {
  if (peers.length > 0) {
    if (peers.includes(currentTarget)) return currentTarget;
    const enabledPeer = servers.find((s) => s.enabled && peers.includes(s.server_id));
    if (enabledPeer !== undefined) return enabledPeer.server_id;
  }
  if (currentTarget === "" || currentTarget === sourceServerId) {
    return firstTargetServer(servers, sourceServerId);
  }
  return currentTarget;
}

function placementSourceLabel(
  servers: ServerRecord[],
  serverId: string,
  remotePath: string,
): string {
  const name = servers.find((s) => s.server_id === serverId)?.display_name ?? serverId;
  return `${name} · ${remotePath}`;
}

function buildRequest(
  artifactId: string,
  placementId: string,
  targetServerId: string,
  targetPath: string,
  strategy: TransferStrategy,
): TransferRequest | null {
  const path = targetPath.trim();
  if (
    artifactId === "" ||
    placementId === "" ||
    targetServerId === "" ||
    path === "" ||
    path.length > 512
  ) {
    return null;
  }
  return {
    artifact_id: artifactId,
    source_placement_id: placementId,
    target_server_id: targetServerId,
    target_path: path,
    strategy,
    verify_mode: "quick" as VerifyMode,
  };
}

export interface NewTransferDialogProps {
  open: boolean;
}

export function NewTransferDialog({ open }: NewTransferDialogProps) {
  const t = useT();
  const prefill = useTransferStore((state) => state.dialog.prefill);
  const closeDialog = useTransferStore((state) => state.closeNewTransfer);
  const createTransfer = useTransferStore((state) => state.createTransfer);

  const artifacts = useWorkspaceStore((state) => state.artifacts);
  const placements = useWorkspaceStore((state) => state.placements);
  const projects = useWorkspaceStore((state) => state.projects);
  const serverRoots = useWorkspaceStore((state) => state.serverRoots);
  const loadServerRoots = useWorkspaceStore((state) => state.loadServerRoots);
  const servers = useConsoleStore((state) => state.servers);

  const [artifactId, setArtifactId] = useState("");
  const [placementId, setPlacementId] = useState("");
  const [targetServerId, setTargetServerId] = useState("");
  const [targetPath, setTargetPath] = useState("");
  const [strategy, setStrategy] = useState<TransferStrategy>("auto");
  const [pathTouched, setPathTouched] = useState(false);
  const [plan, setPlan] = useState<TransferPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // Phase 4.2D: 配置直连 setup dialog + explicit re-plan trigger. planNonce
  // re-runs the debounced plan effect without touching any form field.
  const [setupOpen, setSetupOpen] = useState(false);
  const [directChanged, setDirectChanged] = useState(false);
  const [planNonce, setPlanNonce] = useState(0);
  const planSeq = useRef(0);

  const artifactPlacements = placements.filter((p) => p.artifact_id === artifactId);
  const selectedPlacement = artifactPlacements.find((p) => p.placement_id === placementId);
  const sourceServerId = selectedPlacement?.server_id;

  // Seed the form on open, from the 同步到… prefill when present. Seeding keys
  // off the open transition only — later store loads never clobber edits.
  const seededOpen = useRef(false);
  useEffect(() => {
    if (!open) {
      seededOpen.current = false;
      return;
    }
    if (seededOpen.current) return;
    seededOpen.current = true;
    const artifact =
      (prefill.artifactId !== undefined
        ? artifacts.find((a) => a.artifact_id === prefill.artifactId)
        : undefined) ?? artifacts[0];
    const nextArtifactId = artifact?.artifact_id ?? "";
    const pool = placements.filter((p) => p.artifact_id === nextArtifactId);
    const placement =
      (prefill.sourcePlacementId !== undefined
        ? pool.find((p) => p.placement_id === prefill.sourcePlacementId)
        : undefined) ?? pool[0];
    setArtifactId(nextArtifactId);
    setPlacementId(placement?.placement_id ?? "");
    setTargetServerId(
      suggestedTargetServer(
        "",
        peerServerIds(nextArtifactId, projects, placements, placement?.server_id),
        servers,
        placement?.server_id,
      ),
    );
    setTargetPath("");
    setPathTouched(false);
    setStrategy("auto");
    setPlan(null);
    setPlanError(null);
    setCreateError(null);
    setSetupOpen(false);
    setDirectChanged(false);
    setPlanNonce(0);
  }, [open, prefill, artifacts, placements, projects, servers]);

  // Target path suggestion from the target server's kind root + name:version,
  // only while the user has not typed their own path. Fetches roots on demand.
  useEffect(() => {
    if (!open || pathTouched) return;
    if (targetServerId === "" || targetServerId === sourceServerId) return;
    if (serverRoots[targetServerId] === undefined) {
      void loadServerRoots(targetServerId);
      return;
    }
    const artifact = artifacts.find((a) => a.artifact_id === artifactId);
    const root = serverRoots[targetServerId]?.[kindRootField(artifact?.kind ?? "dataset")];
    if (artifact === undefined || root === null || root === "") return;
    setTargetPath(`${root}/${artifactLabel(artifact)}`);
  }, [
    open,
    pathTouched,
    artifactId,
    targetServerId,
    sourceServerId,
    serverRoots,
    artifacts,
    loadServerRoots,
  ]);

  const request = buildRequest(artifactId, placementId, targetServerId, targetPath, strategy);

  // Debounced plan: complete form state re-runs the availability check after
  // 400 ms; out-of-order responses are dropped by sequence number. planNonce
  // (重新规划) forces one extra run for the unchanged form.
  useEffect(() => {
    if (!open || request === null) {
      setPlan(null);
      return;
    }
    const seq = ++planSeq.current;
    setPlanning(true);
    setPlanError(null);
    const handle = setTimeout(() => {
      transfersApi
        .plan(request)
        .then((result) => {
          if (seq === planSeq.current) setPlan(result);
        })
        .catch((cause: unknown) => {
          if (seq === planSeq.current) {
            setPlan(null);
            setPlanError(cause instanceof Error ? cause.message : "request failed");
          }
        })
        .finally(() => {
          if (seq === planSeq.current) setPlanning(false);
        });
    }, PLAN_DEBOUNCE_MS);
    return () => clearTimeout(handle);
    // request is derived from exactly these fields (+ the explicit re-plan nonce).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, artifactId, placementId, targetServerId, targetPath, strategy, planNonce]);

  const submit = async (): Promise<void> => {
    if (request === null) return;
    setCreating(true);
    setCreateError(null);
    try {
      await createTransfer(request);
      closeDialog();
    } catch (cause) {
      setCreateError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setCreating(false);
    }
  };

  const directAvailable = plan?.strategy_available["direct_rsync"] === true;
  const relayAvailable = plan?.strategy_available["local_relay"] === true;
  const directPair =
    sourceServerId !== undefined &&
    targetServerId !== "" &&
    sourceServerId !== "" &&
    targetServerId !== sourceServerId;

  const sourceName =
    servers.find((s) => s.server_id === sourceServerId)?.display_name ?? sourceServerId ?? "";
  const targetName =
    servers.find((s) => s.server_id === targetServerId)?.display_name ?? targetServerId;

  const replan = () => {
    setDirectChanged(false);
    setPlanNonce((nonce) => nonce + 1);
  };

  return (
    <Dialog open={open} onClose={closeDialog} title={t.transfers.dialogTitle} width={480}>
      <form
        className="tf-dialog__form"
        onSubmit={(event) => {
          event.preventDefault();
          if (request !== null && !creating) void submit();
        }}
      >
        <Field label={t.transfers.artifact} htmlFor="tf-artifact">
          <Select
            id="tf-artifact"
            value={artifactId}
            onChange={(event) => {
              const next = event.target.value;
              setArtifactId(next);
              const nextPlacement = placements.find((p) => p.artifact_id === next);
              const nextSourceServerId = nextPlacement?.server_id;
              // A new artifact voids the previous manual path (the suggestion
              // effect regenerates it) and re-aims the target toward the
              // project's peer servers when they exist; a current target that
              // is already a peer (or, peer-less, is still valid) is kept.
              setPathTouched(false);
              setTargetServerId(
                suggestedTargetServer(
                  targetServerId,
                  peerServerIds(next, projects, placements, nextSourceServerId),
                  servers,
                  nextSourceServerId,
                ),
              );
              setPlacementId(nextPlacement?.placement_id ?? "");
            }}
          >
            {artifacts.length === 0 && <option value="">—</option>}
            {artifacts.map((artifact) => (
              <option key={artifact.artifact_id} value={artifact.artifact_id}>
                {artifactLabel(artifact)}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label={t.transfers.sourcePlacement}
          htmlFor="tf-source"
          hint={artifactPlacements.length === 0 ? t.transfers.noPlacements : undefined}
        >
          <Select
            id="tf-source"
            value={placementId}
            onChange={(event) => {
              const next = event.target.value;
              setPlacementId(next);
              // Switching source must never leave the target unset or equal
              // to the new source; a still-valid target choice is kept.
              const nextServerId = placements.find((p) => p.placement_id === next)?.server_id;
              if (targetServerId === "" || targetServerId === nextServerId) {
                setTargetServerId(firstTargetServer(servers, nextServerId));
              }
            }}
          >
            {artifactPlacements.length === 0 && <option value="">—</option>}
            {artifactPlacements.map((placement) => (
              <option key={placement.placement_id} value={placement.placement_id}>
                {placementSourceLabel(servers, placement.server_id, placement.remote_path)}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label={t.transfers.targetServer}
          htmlFor="tf-target-server"
          hint={servers.length === 0 ? t.transfers.noServers : undefined}
        >
          <Select
            id="tf-target-server"
            value={targetServerId}
            onChange={(event) => setTargetServerId(event.target.value)}
          >
            {servers.length === 0 && <option value="">—</option>}
            {servers.map((server) => (
              <option key={server.server_id} value={server.server_id}>
                {server.display_name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label={t.transfers.targetPath} htmlFor="tf-target-path">
          <TextInput
            id="tf-target-path"
            className="mono"
            spellCheck={false}
            value={targetPath}
            onChange={(event) => {
              setPathTouched(true);
              setTargetPath(event.target.value);
            }}
          />
        </Field>

        <Field label={t.transfers.method}>
          <div className="tf-method" role="group" aria-label={t.transfers.method}>
            {STRATEGY_OPTIONS.map((option) => (
              <button
                key={option}
                type="button"
                className={cx("tf-method__opt", strategy === option && "tf-method__opt--active")}
                aria-pressed={strategy === option}
                onClick={() => setStrategy(option)}
              >
                {strategyLabel(t, option)}
              </button>
            ))}
          </div>
        </Field>

        {(planning || plan !== null || planError !== null) && (
          <div className="tf-plan">
            {planning && <p className="tf-plan__busy">{t.transfers.planning}</p>}
            {plan !== null && (
              <>
                <div className="tf-plan__row">
                  <span>{t.transfers.strategyDirect}</span>
                  <Chip tone={directAvailable ? "ok" : "crit"}>
                    {directAvailable ? t.transfers.available : t.transfers.unavailable}
                  </Chip>
                </div>
                {!directAvailable && (
                  <>
                    <p className="tf-plan__reason">
                      {tf(t.transfers.reasonLead, { text: t.transfers.authFailReason })}
                    </p>
                    {directPair && (
                      <div className="tf-plan__direct">
                        <Button onClick={() => setSetupOpen(true)}>
                          {t.transfers.configureDirect}
                        </Button>
                        {directChanged && (
                          <Button variant="primary" onClick={replan}>
                            {t.transfers.replan}
                          </Button>
                        )}
                      </div>
                    )}
                  </>
                )}
                <div className="tf-plan__row">
                  <span>{t.transfers.strategyRelay}</span>
                  <Chip tone={relayAvailable ? "ok" : "crit"}>
                    {relayAvailable ? t.transfers.available : t.transfers.unavailable}
                  </Chip>
                </div>
                {plan.reason !== "" && <p className="tf-plan__reason mono">{plan.reason}</p>}
                {plan.space_warning !== "" ? (
                  <p className="tf-plan__space-warning">{plan.space_warning}</p>
                ) : plan.target_free_b !== null ? (
                  <p className="tf-plan__free mono">
                    {tf(t.transfers.targetFree, { size: formatBytes(plan.target_free_b) })}
                  </p>
                ) : null}
                {plan.excludes.length > 0 && (
                  <div className="tf-plan__excludes">
                    <span className="tf-plan__reason">{t.transfers.planExcludes}</span>
                    <span className="tf-plan__chips">
                      {plan.excludes.map((pattern) => (
                        <Chip key={pattern} mono>
                          {pattern}
                        </Chip>
                      ))}
                    </span>
                  </div>
                )}
              </>
            )}
            {!planning && plan === null && planError !== null && (
              <p className="field__error">{planError}</p>
            )}
          </div>
        )}

        {createError !== null && <p className="field__error">{createError}</p>}

        <div className="ws-dialog__actions">
          <Button onClick={closeDialog} disabled={creating}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" type="submit" disabled={request === null || creating}>
            {creating ? t.transfers.creating : t.transfers.create}
          </Button>
        </div>
      </form>

      {directPair && sourceServerId !== undefined && (
        <DirectAuthSetupDialog
          open={setupOpen}
          onClose={() => setSetupOpen(false)}
          sourceId={sourceServerId}
          sourceName={sourceName}
          targetId={targetServerId}
          targetName={targetName}
          onReplan={replan}
        />
      )}
    </Dialog>
  );
}

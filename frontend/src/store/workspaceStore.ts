/**
 * Workspace catalog store (Phase 1): projects / artifacts / placements /
 * launch configs lists, per-server roots cache, RAM-only placement
 * inspections, and the Phase 4E per-project distribution cache with the
 * inspect / sync-plan / sync actions. REST only — loads happen on demand
 * (page open, tab refresh, after mutations); there is deliberately NO polling
 * and NO persistence. Distribution GETs never trigger SSH; inspect/sync-plan/
 * sync are explicit user actions.
 */

import { create } from "zustand";
import { workspaceApi } from "../services/workspaceApi";
import type { TransferBatch } from "../types/transfers";
import type {
  ArtifactCreate,
  ArtifactPatch,
  ArtifactRecord,
  LaunchConfigCreate,
  LaunchConfigPatch,
  LaunchConfigRecord,
  PlacementCreate,
  PlacementInspection,
  PlacementPatch,
  PlacementRecord,
  ProjectCreate,
  ProjectDistribution,
  ProjectPatch,
  ProjectRecord,
  ProjectSyncPlan,
  ServerRoots,
  ServerRootsUpdate,
  SyncPlanRequest,
} from "../types/workspace";

export interface WorkspaceState {
  projects: ProjectRecord[];
  projectsLoading: boolean;
  projectsError: string | null;
  artifacts: ArtifactRecord[];
  artifactsLoading: boolean;
  artifactsError: string | null;
  placements: PlacementRecord[];
  placementsLoading: boolean;
  placementsError: string | null;
  launchConfigs: LaunchConfigRecord[];
  launchConfigsLoading: boolean;
  launchConfigsError: string | null;
  /** Per-server declared roots: server_id → roots (null field = unset). */
  serverRoots: Record<string, ServerRoots>;
  rootsLoading: Record<string, boolean>;
  rootsErrors: Record<string, string>;
  /** RAM-only inspection results, keyed by placement id; replaced per inspect. */
  inspections: Record<string, PlacementInspection>;
  inspecting: Record<string, boolean>;
  inspectErrors: Record<string, string>;
  /** Phase 4E: distribution snapshots cached per project id (GET only). */
  distributions: Record<string, ProjectDistribution>;
  distributionLoading: Record<string, boolean>;
  distributionErrors: Record<string, string>;
  /** Explicit project-wide inspect (SSH); keyed by project id. */
  projectInspecting: Record<string, boolean>;
  projectInspectErrors: Record<string, string>;
  /** Sync plan for the one open SyncDialog; null until a target is chosen. */
  syncPlan: ProjectSyncPlan | null;
  syncPlanLoading: boolean;
  syncPlanError: string | null;
  /** While POST /sync is in flight. */
  syncRunning: boolean;
  syncError: string | null;

  loadWorkspace: () => Promise<void>;
  loadProjects: () => Promise<void>;
  loadArtifacts: () => Promise<void>;
  loadPlacements: () => Promise<void>;
  loadLaunchConfigs: () => Promise<void>;
  loadServerRoots: (serverId: string) => Promise<void>;

  createProject: (body: ProjectCreate) => Promise<ProjectRecord>;
  patchProject: (projectId: string, patch: ProjectPatch) => Promise<ProjectRecord>;
  deleteProject: (projectId: string) => Promise<void>;

  createArtifact: (body: ArtifactCreate) => Promise<ArtifactRecord>;
  patchArtifact: (artifactId: string, patch: ArtifactPatch) => Promise<ArtifactRecord>;
  deleteArtifact: (artifactId: string) => Promise<void>;

  createPlacement: (body: PlacementCreate) => Promise<PlacementRecord>;
  patchPlacement: (placementId: string, patch: PlacementPatch) => Promise<PlacementRecord>;
  deletePlacement: (placementId: string) => Promise<void>;

  createLaunchConfig: (body: LaunchConfigCreate) => Promise<LaunchConfigRecord>;
  patchLaunchConfig: (
    launchConfigId: string,
    patch: LaunchConfigPatch,
  ) => Promise<LaunchConfigRecord>;
  deleteLaunchConfig: (launchConfigId: string) => Promise<void>;

  inspectPlacement: (placementId: string) => Promise<PlacementInspection>;
  saveServerRoots: (serverId: string, update: ServerRootsUpdate) => Promise<ServerRoots>;

  /** Read-only distribution fetch (no SSH); caches per project. */
  fetchDistribution: (projectId: string) => Promise<void>;
  /** Explicit SSH check; replaces the cached distribution with the response. */
  inspectProject: (projectId: string) => Promise<ProjectDistribution>;
  buildSyncPlan: (projectId: string, body: SyncPlanRequest) => Promise<ProjectSyncPlan>;
  /** Executes the sync; throws ApiError (409 message verbatim) on refusal. */
  syncProject: (projectId: string, body: SyncPlanRequest) => Promise<TransferBatch>;
  /** Clears plan/run state when the SyncDialog closes. */
  resetSyncPlan: () => void;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "request failed";
}

export const createWorkspaceStore = () =>
  create<WorkspaceState>()((set, get) => ({
    projects: [],
    projectsLoading: false,
    projectsError: null,
    artifacts: [],
    artifactsLoading: false,
    artifactsError: null,
    placements: [],
    placementsLoading: false,
    placementsError: null,
    launchConfigs: [],
    launchConfigsLoading: false,
    launchConfigsError: null,
    serverRoots: {},
    rootsLoading: {},
    rootsErrors: {},
    inspections: {},
    inspecting: {},
    inspectErrors: {},
    distributions: {},
    distributionLoading: {},
    distributionErrors: {},
    projectInspecting: {},
    projectInspectErrors: {},
    syncPlan: null,
    syncPlanLoading: false,
    syncPlanError: null,
    syncRunning: false,
    syncError: null,

    loadWorkspace: async () => {
      await Promise.all([
        get().loadProjects(),
        get().loadArtifacts(),
        get().loadPlacements(),
        get().loadLaunchConfigs(),
      ]);
    },

    loadProjects: async () => {
      set({ projectsLoading: true, projectsError: null });
      try {
        const projects = await workspaceApi.listProjects();
        set({ projects, projectsLoading: false });
      } catch (cause) {
        set({ projectsError: errorMessage(cause), projectsLoading: false });
      }
    },

    loadArtifacts: async () => {
      set({ artifactsLoading: true, artifactsError: null });
      try {
        const artifacts = await workspaceApi.listArtifacts();
        set({ artifacts, artifactsLoading: false });
      } catch (cause) {
        set({ artifactsError: errorMessage(cause), artifactsLoading: false });
      }
    },

    loadPlacements: async () => {
      set({ placementsLoading: true, placementsError: null });
      try {
        const placements = await workspaceApi.listPlacements();
        set({ placements, placementsLoading: false });
      } catch (cause) {
        set({ placementsError: errorMessage(cause), placementsLoading: false });
      }
    },

    loadLaunchConfigs: async () => {
      set({ launchConfigsLoading: true, launchConfigsError: null });
      try {
        const launchConfigs = await workspaceApi.listLaunchConfigs();
        set({ launchConfigs, launchConfigsLoading: false });
      } catch (cause) {
        set({ launchConfigsError: errorMessage(cause), launchConfigsLoading: false });
      }
    },

    loadServerRoots: async (serverId) => {
      set({
        rootsLoading: { ...get().rootsLoading, [serverId]: true },
        rootsErrors: { ...get().rootsErrors, [serverId]: "" },
      });
      try {
        const roots = await workspaceApi.getServerRoots(serverId);
        set({
          serverRoots: { ...get().serverRoots, [serverId]: roots },
          rootsLoading: { ...get().rootsLoading, [serverId]: false },
        });
      } catch (cause) {
        set({
          rootsErrors: { ...get().rootsErrors, [serverId]: errorMessage(cause) },
          rootsLoading: { ...get().rootsLoading, [serverId]: false },
        });
      }
    },

    createProject: async (body) => {
      const record = await workspaceApi.createProject(body);
      set({ projects: [...get().projects, record] });
      return record;
    },

    patchProject: async (projectId, patch) => {
      const record = await workspaceApi.patchProject(projectId, patch);
      set({
        projects: get().projects.map((p) => (p.project_id === projectId ? record : p)),
      });
      return record;
    },

    deleteProject: async (projectId) => {
      await workspaceApi.deleteProject(projectId);
      const project = get().projects.find((p) => p.project_id === projectId);
      const distributions = { ...get().distributions };
      delete distributions[projectId];
      set({
        projects: get().projects.filter((p) => p.project_id !== projectId),
        distributions,
        // Project deletion cascades its launch configs server-side.
        launchConfigs: get().launchConfigs.filter(
          (config) =>
            project === undefined || !project.launch_config_ids.includes(config.launch_config_id),
        ),
      });
    },

    createArtifact: async (body) => {
      const record = await workspaceApi.createArtifact(body);
      set({ artifacts: [...get().artifacts, record] });
      return record;
    },

    patchArtifact: async (artifactId, patch) => {
      const record = await workspaceApi.patchArtifact(artifactId, patch);
      set({
        artifacts: get().artifacts.map((a) => (a.artifact_id === artifactId ? record : a)),
      });
      return record;
    },

    deleteArtifact: async (artifactId) => {
      await workspaceApi.deleteArtifact(artifactId);
      const inspections = { ...get().inspections };
      for (const placement of get().placements) {
        if (placement.artifact_id === artifactId) delete inspections[placement.placement_id];
      }
      set({
        artifacts: get().artifacts.filter((a) => a.artifact_id !== artifactId),
        placements: get().placements.filter((p) => p.artifact_id !== artifactId),
        inspections,
      });
    },

    createPlacement: async (body) => {
      const record = await workspaceApi.createPlacement(body);
      set({ placements: [...get().placements, record] });
      return record;
    },

    patchPlacement: async (placementId, patch) => {
      const record = await workspaceApi.patchPlacement(placementId, patch);
      set({
        placements: get().placements.map((p) => (p.placement_id === placementId ? record : p)),
      });
      return record;
    },

    deletePlacement: async (placementId) => {
      await workspaceApi.deletePlacement(placementId);
      const inspections = { ...get().inspections };
      delete inspections[placementId];
      set({
        placements: get().placements.filter((p) => p.placement_id !== placementId),
        inspections,
      });
    },

    createLaunchConfig: async (body) => {
      const record = await workspaceApi.createLaunchConfig(body);
      set({ launchConfigs: [...get().launchConfigs, record] });
      void get().loadProjects(); // project.launch_config_ids changed server-side
      return record;
    },

    patchLaunchConfig: async (launchConfigId, patch) => {
      const record = await workspaceApi.patchLaunchConfig(launchConfigId, patch);
      set({
        launchConfigs: get().launchConfigs.map((l) =>
          l.launch_config_id === launchConfigId ? record : l,
        ),
      });
      return record;
    },

    deleteLaunchConfig: async (launchConfigId) => {
      await workspaceApi.deleteLaunchConfig(launchConfigId);
      set({
        launchConfigs: get().launchConfigs.filter((l) => l.launch_config_id !== launchConfigId),
      });
      void get().loadProjects(); // project.launch_config_ids changed server-side
    },

    inspectPlacement: async (placementId) => {
      set({ inspecting: { ...get().inspecting, [placementId]: true } });
      try {
        const inspection = await workspaceApi.inspectPlacement(placementId);
        set({
          inspections: { ...get().inspections, [placementId]: inspection },
          inspectErrors: { ...get().inspectErrors, [placementId]: "" },
        });
        return inspection;
      } catch (cause) {
        set({
          inspectErrors: {
            ...get().inspectErrors,
            [placementId]: errorMessage(cause),
          },
        });
        throw cause;
      } finally {
        set({ inspecting: { ...get().inspecting, [placementId]: false } });
      }
    },

    saveServerRoots: async (serverId, update) => {
      const roots = await workspaceApi.putServerRoots(serverId, update);
      set({ serverRoots: { ...get().serverRoots, [serverId]: roots } });
      return roots;
    },

    fetchDistribution: async (projectId) => {
      set({
        distributionLoading: { ...get().distributionLoading, [projectId]: true },
        distributionErrors: { ...get().distributionErrors, [projectId]: "" },
      });
      try {
        const distribution = await workspaceApi.getProjectDistribution(projectId);
        set({
          distributions: { ...get().distributions, [projectId]: distribution },
          distributionLoading: { ...get().distributionLoading, [projectId]: false },
        });
      } catch (cause) {
        set({
          distributionErrors: {
            ...get().distributionErrors,
            [projectId]: errorMessage(cause),
          },
          distributionLoading: { ...get().distributionLoading, [projectId]: false },
        });
      }
    },

    inspectProject: async (projectId) => {
      set({
        projectInspecting: { ...get().projectInspecting, [projectId]: true },
        projectInspectErrors: { ...get().projectInspectErrors, [projectId]: "" },
      });
      try {
        const distribution = await workspaceApi.inspectProject(projectId);
        set({
          distributions: { ...get().distributions, [projectId]: distribution },
          projectInspecting: { ...get().projectInspecting, [projectId]: false },
        });
        return distribution;
      } catch (cause) {
        set({
          projectInspectErrors: {
            ...get().projectInspectErrors,
            [projectId]: errorMessage(cause),
          },
          projectInspecting: { ...get().projectInspecting, [projectId]: false },
        });
        throw cause;
      }
    },

    buildSyncPlan: async (projectId, body) => {
      // Drop the previous plan immediately: a stale plan for another target
      // server must never be confirmable while the new one loads.
      set({ syncPlan: null, syncPlanLoading: true, syncPlanError: null });
      try {
        const plan = await workspaceApi.buildSyncPlan(projectId, body);
        set({ syncPlan: plan, syncPlanLoading: false });
        return plan;
      } catch (cause) {
        set({ syncPlanError: errorMessage(cause), syncPlanLoading: false });
        throw cause;
      }
    },

    syncProject: async (projectId, body) => {
      set({ syncRunning: true, syncError: null });
      try {
        const batch = await workspaceApi.syncProject(projectId, body);
        set({ syncRunning: false });
        return batch;
      } catch (cause) {
        set({ syncRunning: false, syncError: errorMessage(cause) });
        throw cause;
      }
    },

    resetSyncPlan: () => {
      set({ syncPlan: null, syncPlanError: null, syncError: null });
    },
  }));

export type WorkspaceStoreApi = ReturnType<typeof createWorkspaceStore>;

/** App-wide singleton. Tests use createWorkspaceStore() for a fresh instance. */
export const useWorkspaceStore = createWorkspaceStore();

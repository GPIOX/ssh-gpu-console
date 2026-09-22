/**
 * Workspace Phase 1: hash-route parsing, the workspace store's CRUD caching
 * and cross-entity invalidation, the ProjectDetail distribution matrix built
 * from placement declarations, dialog payload shapes (project / artifact /
 * launch config), and the zh/en workspace dictionary parity.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { parseHash, routeToHash } from "../shell/routes";
import { workspaceApi } from "../services/workspaceApi";
import { transfersApi } from "../services/transfersApi";
import { createWorkspaceStore, useWorkspaceStore } from "../store/workspaceStore";
import { useConsoleStore } from "../store/consoleStore";
import { useTransferStore } from "../store/transferStore";
import { en } from "../i18n/en";
import { zh } from "../i18n/zh";
import type { ServerRecord } from "../types/models";
import type {
  ArtifactRecord,
  DistributionItem,
  LaunchConfigRecord,
  PlacementRecord,
  ProjectDistribution,
  ProjectRecord,
} from "../types/workspace";
import { WorkspacePage } from "../features/workspace/WorkspacePage";
import { ProjectDetail } from "../features/workspace/ProjectDetail";
import { ProjectDialog } from "../features/workspace/ProjectDialog";
import { ProjectExcludesDialog } from "../features/workspace/ProjectExcludesDialog";
import { ArtifactDialog } from "../features/workspace/ArtifactDialog";
import { LaunchConfigDialog } from "../features/workspace/LaunchConfigDialog";
import { PlacementTable } from "../features/workspace/PlacementTable";

const NOW = "2026-09-17T12:00:00Z";

function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "p1",
    name: "dinov2-linear",
    description: "Linear probe on DINOv2",
    artifact_ids: [],
    launch_config_ids: [],
    tags: ["cv"],
    transfer_excludes: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeArtifact(overrides: Partial<ArtifactRecord> = {}): ArtifactRecord {
  return {
    artifact_id: "a1",
    kind: "dataset",
    name: "DINOv2",
    version: "b",
    description: "",
    immutable: true,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makePlacement(overrides: Partial<PlacementRecord> = {}): PlacementRecord {
  return {
    placement_id: "pl1",
    artifact_id: "a1",
    server_id: "srv-a",
    remote_path: "/data/dinov2",
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeLaunchConfig(overrides: Partial<LaunchConfigRecord> = {}): LaunchConfigRecord {
  return {
    launch_config_id: "lc1",
    project_id: "p1",
    name: "train",
    working_dir: "/home/demo/dinov2",
    program: "python",
    args: ["-m", "train"],
    environment: "torch",
    env_vars: { CUDA_VISIBLE_DEVICES: "0" },
    required_artifact_ids: ["a1"],
    gpu_count: 2,
    min_vram_b: 2147483648,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeDistributionItem(overrides: Partial<DistributionItem> = {}): DistributionItem {
  return {
    artifact_id: "a1",
    artifact_label: "DINOv2:b",
    artifact_kind: "dataset",
    server_id: "srv-a",
    placement_id: "pl1",
    remote_path: "/data/dinov2",
    state: "declared",
    checked_at: null,
    detail: null,
    active_transfer_job_id: null,
    ...overrides,
  };
}

function makeDistribution(overrides: Partial<ProjectDistribution> = {}): ProjectDistribution {
  return {
    project_id: "p1",
    generated_at: NOW,
    items: [],
    ...overrides,
  };
}

function resetWorkspace(): void {
  useWorkspaceStore.setState({
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
  });
}

function resetConsole(): void {
  useConsoleStore.setState({ servers: [], statuses: {} });
}

describe("workspace hash routes", () => {
  it("parses sections with defaults", () => {
    expect(parseHash("#/workspace/projects")).toEqual({
      name: "workspace",
      section: "projects",
      projectId: undefined,
      artifactId: undefined,
    });
    expect(parseHash("#/workspace")).toEqual({
      name: "workspace",
      section: "projects",
      projectId: undefined,
      artifactId: undefined,
    });
    expect(parseHash("#/workspace/bogus")).toEqual({
      name: "workspace",
      section: "projects",
      projectId: undefined,
      artifactId: undefined,
    });
    expect(parseHash("#/workspace/datasets")).toEqual({
      name: "workspace",
      section: "datasets",
      projectId: undefined,
      artifactId: undefined,
    });
    expect(parseHash("#/workspace/models")).toEqual({
      name: "workspace",
      section: "models",
      projectId: undefined,
      artifactId: undefined,
    });
    expect(parseHash("#/workspace/launches")).toEqual({
      name: "workspace",
      section: "launches",
      projectId: undefined,
      artifactId: undefined,
    });
  });

  it("parses project and dataset detail ids with decoding", () => {
    expect(parseHash("#/workspace/projects/p1")).toEqual({
      name: "workspace",
      section: "projects",
      projectId: "p1",
      artifactId: undefined,
    });
    expect(parseHash("#/workspace/datasets/d%20s")).toEqual({
      name: "workspace",
      section: "datasets",
      projectId: undefined,
      artifactId: "d s",
    });
  });

  it("round-trips through routeToHash", () => {
    const detail = { name: "workspace", section: "projects", projectId: "p x" } as const;
    expect(parseHash(routeToHash(detail))).toEqual({
      ...detail,
      artifactId: undefined,
    });
    expect(parseHash(routeToHash({ name: "workspace", section: "launches" }))).toEqual({
      name: "workspace",
      section: "launches",
      projectId: undefined,
      artifactId: undefined,
    });
  });

  it("keeps fleet the fallback for unknown heads", () => {
    expect(parseHash("#/nope")).toEqual({ name: "fleet" });
  });
});

describe("workspaceStore CRUD caching", () => {
  let store: ReturnType<typeof createWorkspaceStore>;

  beforeEach(() => {
    store = createWorkspaceStore();
  });

  it("loads all four catalogs and clears loading flags", async () => {
    const projects = [makeProject()];
    const artifacts = [makeArtifact()];
    const placements = [makePlacement()];
    const configs = [makeLaunchConfig()];
    vi.spyOn(workspaceApi, "listProjects").mockResolvedValue(projects);
    vi.spyOn(workspaceApi, "listArtifacts").mockResolvedValue(artifacts);
    vi.spyOn(workspaceApi, "listPlacements").mockResolvedValue(placements);
    vi.spyOn(workspaceApi, "listLaunchConfigs").mockResolvedValue(configs);

    await store.getState().loadWorkspace();

    expect(store.getState().projects).toEqual(projects);
    expect(store.getState().artifacts).toEqual(artifacts);
    expect(store.getState().placements).toEqual(placements);
    expect(store.getState().launchConfigs).toEqual(configs);
    expect(store.getState().projectsLoading).toBe(false);
  });

  it("surfaces list failures as error strings without throwing", async () => {
    vi.spyOn(workspaceApi, "listProjects").mockRejectedValue(new Error("backend down"));
    await store.getState().loadProjects();
    expect(store.getState().projectsError).toBe("backend down");
    expect(store.getState().projectsLoading).toBe(false);
  });

  it("appends created records and replaces patched ones", async () => {
    vi.spyOn(workspaceApi, "createProject").mockResolvedValue(
      makeProject({ project_id: "p2", name: "second" }),
    );
    const created = await store.getState().createProject({
      name: "second",
      description: "",
      artifact_ids: [],
      tags: [],
    });
    expect(created.name).toBe("second");
    expect(store.getState().projects.map((p) => p.project_id)).toEqual(["p2"]);

    store.setState({ projects: [makeProject()] });
    vi.spyOn(workspaceApi, "patchProject").mockResolvedValue(makeProject({ name: "renamed" }));
    await store.getState().patchProject("p1", { name: "renamed" });
    expect(store.getState().projects[0]?.name).toBe("renamed");
  });

  it("deleting a project also drops its launch configs (backend cascade)", async () => {
    vi.spyOn(workspaceApi, "deleteProject").mockResolvedValue(undefined);
    store.setState({
      projects: [makeProject({ launch_config_ids: ["lc1"] })],
      launchConfigs: [makeLaunchConfig(), makeLaunchConfig({ launch_config_id: "lc2" })],
    });
    await store.getState().deleteProject("p1");
    expect(store.getState().projects).toEqual([]);
    expect(store.getState().launchConfigs.map((c) => c.launch_config_id)).toEqual(["lc2"]);
  });

  it("deleting an artifact drops its placements and inspections", async () => {
    vi.spyOn(workspaceApi, "deleteArtifact").mockResolvedValue(undefined);
    store.setState({
      artifacts: [makeArtifact(), makeArtifact({ artifact_id: "a2", kind: "model" })],
      placements: [makePlacement(), makePlacement({ artifact_id: "a2", server_id: "srv-b" })],
      inspections: { pl1: { placement_id: "pl1", state: "verified", file_type: null, size_b: 1, file_count: null, checked_at: NOW, detail: "" } },
    });
    await store.getState().deleteArtifact("a1");
    expect(store.getState().artifacts.map((a) => a.artifact_id)).toEqual(["a2"]);
    expect(store.getState().placements.map((p) => p.server_id)).toEqual(["srv-b"]);
    expect(store.getState().inspections).toEqual({});
  });

  it("caches inspection results per placement and reports failures", async () => {
    vi.spyOn(workspaceApi, "inspectPlacement").mockResolvedValue({
      placement_id: "pl1",
      state: "verified",
      file_type: "directory",
      size_b: 1024,
      file_count: 7,
      checked_at: NOW,
      detail: "",
    });
    await store.getState().inspectPlacement("pl1");
    expect(store.getState().inspections["pl1"]?.state).toBe("verified");
    expect(store.getState().inspecting["pl1"]).toBe(false);

    vi.spyOn(workspaceApi, "inspectPlacement").mockRejectedValue(new Error("ssh timeout"));
    await expect(store.getState().inspectPlacement("pl1")).rejects.toThrow("ssh timeout");
    expect(store.getState().inspectErrors["pl1"]).toBe("ssh timeout");
  });

  it("saving server roots caches the response", async () => {
    const roots = {
      project_root: "/proj",
      dataset_root: "/data",
      model_root: null,
      output_root: null,
    };
    vi.spyOn(workspaceApi, "putServerRoots").mockResolvedValue(roots);
    await store.getState().saveServerRoots("srv-a", {
      project_root: "/proj",
      dataset_root: "/data",
      model_root: null,
      output_root: null,
    });
    expect(store.getState().serverRoots["srv-a"]).toEqual(roots);
    expect(workspaceApi.putServerRoots).toHaveBeenCalledWith("srv-a", {
      project_root: "/proj",
      dataset_root: "/data",
      model_root: null,
      output_root: null,
    });
  });
});

describe("ProjectDetail distribution matrix", () => {
  beforeEach(() => {
    resetWorkspace();
    resetConsole();
    const servers: ServerRecord[] = [
      { server_id: "srv-a", display_name: "lab-4090", ssh_host: "10.0.0.8", username: null, port: 22, tags: [], enabled: true },
      { server_id: "srv-b", display_name: "one4090", ssh_host: "10.0.0.9", username: null, port: 22, tags: [], enabled: true },
    ];
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      projects: [makeProject({ artifact_ids: ["a1", "a2"] })],
      artifacts: [makeArtifact(), makeArtifact({ artifact_id: "a2", kind: "model", name: "SAM", version: null })],
      placements: [makePlacement()],
      launchConfigs: [makeLaunchConfig()],
    });
    // ProjectDetail fetches the read-only snapshot on mount; keep tests hermetic.
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(
      makeDistribution({ items: [makeDistributionItem({ state: "verified" })] }),
    );
  });

  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("renders registry servers as columns and snapshot states as cells", async () => {
    render(<ProjectDetail projectId="p1" />);
    const matrix = screen.getByRole("table");
    expect(within(matrix).getByText("lab-4090")).toBeTruthy();
    expect(within(matrix).getByText("one4090")).toBeTruthy();
    expect(within(matrix).getByText("DINOv2:b")).toBeTruthy();
    // 4 cells: a1 on srv-a verified per the snapshot; the other three have no
    // declared placement. Cell aria-labels read "artifact / server / state".
    expect(await screen.findByLabelText("DINOv2:b / lab-4090 / verified")).toBeTruthy();
    expect(screen.getAllByLabelText(/\/ verified$/)).toHaveLength(1);
    expect(screen.getAllByLabelText(/\/ not placed$/)).toHaveLength(3);
  });

  it("lists the project's launch configs as mono program+args with a GPU chip", () => {
    render(<ProjectDetail projectId="p1" />);
    expect(screen.getByText("train")).toBeTruthy();
    expect(screen.getByText("python -m train")).toBeTruthy();
    expect(screen.getByText("GPU ×2")).toBeTruthy();
  });

  it("shows the not-found state for an unknown project", () => {
    render(<ProjectDetail projectId="ghost" />);
    expect(screen.getByText("Project not found")).toBeTruthy();
  });
});

describe("PlacementTable inspection", () => {
  beforeEach(() => {
    resetWorkspace();
    resetConsole();
    useConsoleStore.setState({
      servers: [
        {
          server_id: "srv-a",
          display_name: "lab-4090",
          ssh_host: "10.0.0.8",
          username: null,
          port: 22,
          tags: [],
          enabled: true,
        },
      ],
      statuses: {},
    });
    useWorkspaceStore.setState({
      artifacts: [makeArtifact()],
      placements: [makePlacement()],
    });
  });

  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("shows the server name, mono remote path, and caches the inspection", async () => {
    vi.spyOn(workspaceApi, "inspectPlacement").mockResolvedValue({
      placement_id: "pl1",
      state: "verified",
      file_type: "directory",
      size_b: 1024,
      file_count: 7,
      checked_at: NOW,
      detail: "",
    });
    render(
      <PlacementTable
        artifacts={[makeArtifact()]}
        placements={[makePlacement()]}
        onEdit={() => undefined}
        onRemove={() => undefined}
      />,
    );

    expect(screen.getByText("lab-4090")).toBeTruthy();
    expect(screen.getByText("/data/dinov2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Inspect" }));
    expect(await screen.findByText("verified")).toBeTruthy();
    expect(screen.getByText("7 files")).toBeTruthy();
    expect(screen.getByText("1.0 KB")).toBeTruthy();
  });
});

describe("dialog payload shapes", () => {
  beforeEach(() => {
    resetWorkspace();
    resetConsole();
  });

  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("create project payload carries name, description, artifact ids and tags", async () => {
    const createSpy = vi
      .spyOn(workspaceApi, "createProject")
      .mockResolvedValue(makeProject({ project_id: "p2" }));
    useWorkspaceStore.setState({ artifacts: [makeArtifact()] });

    render(<ProjectDialog open onClose={() => undefined} project={null} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "dinov2-linear" } });
    fireEvent.change(screen.getByLabelText("Description"), {
      target: { value: "Linear probing" },
    });
    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "cv, probe" } });
    fireEvent.click(screen.getByLabelText("DINOv2:b"));
    fireEvent.click(screen.getByRole("button", { name: "Add project" }));

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        name: "dinov2-linear",
        description: "Linear probing",
        artifact_ids: ["a1"],
        tags: ["cv", "probe"],
      });
    });
  });

  it("edit project patches the same shape against the project id", async () => {
    const patchSpy = vi
      .spyOn(workspaceApi, "patchProject")
      .mockResolvedValue(makeProject({ name: "renamed" }));
    useWorkspaceStore.setState({ artifacts: [makeArtifact()] });

    render(<ProjectDialog open onClose={() => undefined} project={makeProject()} />);
    const name = screen.getByLabelText("Name") as HTMLInputElement;
    expect(name.value).toBe("dinov2-linear");
    fireEvent.change(name, { target: { value: "renamed" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith("p1", {
        name: "renamed",
        description: "Linear probe on DINOv2",
        artifact_ids: [],
        tags: ["cv"],
      });
    });
  });

  it("create artifact payload defaults immutable for datasets and allows version", async () => {
    const createSpy = vi
      .spyOn(workspaceApi, "createArtifact")
      .mockResolvedValue(makeArtifact({ artifact_id: "a2" }));

    render(<ArtifactDialog open onClose={() => undefined} kind="dataset" artifact={null} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "ImageNet" } });
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "v1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add artifact" }));

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        kind: "dataset",
        name: "ImageNet",
        version: "v1",
        description: "",
        immutable: true,
      });
    });
  });

  it("launch config payload is structured: program, comma args, env rows, GiB→bytes", async () => {
    const createSpy = vi
      .spyOn(workspaceApi, "createLaunchConfig")
      .mockResolvedValue(makeLaunchConfig({ launch_config_id: "lc2" }));
    useWorkspaceStore.setState({ artifacts: [makeArtifact()] });

    render(
      <LaunchConfigDialog
        open
        onClose={() => undefined}
        config={null}
        projects={[makeProject()]}
        project={null}
      />,
    );
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "p1" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "train" } });
    fireEvent.change(screen.getByLabelText("Program"), { target: { value: "python" } });
    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: "-m, train" } });
    fireEvent.change(screen.getByLabelText("Environment"), { target: { value: "torch" } });
    fireEvent.click(screen.getByRole("button", { name: "Add variable" }));
    fireEvent.change(screen.getByLabelText("Variable 1"), { target: { value: "CUDA_VISIBLE_DEVICES" } });
    fireEvent.change(screen.getByLabelText("Value 1"), { target: { value: "0" } });
    fireEvent.change(screen.getByLabelText("GPU count"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Min VRAM (GiB)"), { target: { value: "2" } });
    fireEvent.click(screen.getByLabelText("DINOv2:b"));
    fireEvent.click(screen.getByRole("button", { name: "Add launch config" }));

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        project_id: "p1",
        name: "train",
        working_dir: null,
        program: "python",
        args: ["-m", "train"],
        environment: "torch",
        env_vars: { CUDA_VISIBLE_DEVICES: "0" },
        required_artifact_ids: ["a1"],
        gpu_count: 2,
        min_vram_b: 2147483648,
      });
    });
  });

  it("blocks submit while the program contains shell metacharacters", () => {
    useWorkspaceStore.setState({ artifacts: [makeArtifact()] });
    render(
      <LaunchConfigDialog
        open
        onClose={() => undefined}
        config={null}
        projects={[makeProject()]}
        project={makeProject()}
      />,
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "train" } });
    fireEvent.change(screen.getByLabelText("Program"), {
      target: { value: "python; rm -rf /" },
    });
    const submit = screen.getByRole("button", { name: "Add launch config" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });
});

describe("project sync excludes editor", () => {
  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("opens with the current patterns and PATCHes the parsed comma list", async () => {
    const patchSpy = vi
      .spyOn(workspaceApi, "patchProject")
      .mockResolvedValue(makeProject({ transfer_excludes: ["dataset", "checkpoints"] }));

    render(
      <ProjectExcludesDialog
        open
        onClose={() => undefined}
        project={makeProject({ transfer_excludes: ["dataset", "checkpoints"] })}
      />,
    );
    const field = screen.getByLabelText("Default sync excludes") as HTMLInputElement;
    expect(field.value).toBe("dataset, checkpoints");

    fireEvent.change(field, { target: { value: "dataset, checkpoints, *.pth, .git" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith("p1", {
        transfer_excludes: ["dataset", "checkpoints", "*.pth", ".git"],
      });
    });
  });

  it("clears the list when the field is emptied", async () => {
    const patchSpy = vi
      .spyOn(workspaceApi, "patchProject")
      .mockResolvedValue(makeProject());
    render(
      <ProjectExcludesDialog
        open
        onClose={() => undefined}
        project={makeProject({ transfer_excludes: ["dataset"] })}
      />,
    );
    fireEvent.change(screen.getByLabelText("Default sync excludes"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith("p1", { transfer_excludes: [] });
    });
  });
});

describe("workspace matrix kind groups + placement hierarchy", () => {
  // Six artifacts across the three kinds; the long model name exercises the
  // artifact-cell title/ellipsis. FAKE names only.
  const artifacts: ArtifactRecord[] = [
    makeArtifact({ artifact_id: "c1", kind: "code", name: "probe-scaffold", version: null }),
    makeArtifact({ artifact_id: "c2", kind: "code", name: "eval-harness", version: "r2" }),
    makeArtifact({ artifact_id: "d1", kind: "dataset", name: "corpus-alpha", version: null }),
    makeArtifact({ artifact_id: "d2", kind: "dataset", name: "corpus-beta", version: "v9" }),
    makeArtifact({
      artifact_id: "m1",
      kind: "model",
      name: "mit-b4-prima-full--ablation-feature-router",
      version: null,
    }),
    makeArtifact({ artifact_id: "m2", kind: "model", name: "tiny-seg", version: "v1" }),
  ];

  const servers: ServerRecord[] = [
    { server_id: "srv-a", display_name: "lab-4090", ssh_host: "10.0.0.8", username: null, port: 22, tags: [], enabled: true },
    { server_id: "srv-b", display_name: "one4090", ssh_host: "10.0.0.9", username: null, port: 22, tags: [], enabled: true },
  ];

  function setupWorkspace(artifactIds: string[], items: DistributionItem[]): void {
    resetWorkspace();
    resetConsole();
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      projects: [makeProject({ artifact_ids: artifactIds })],
      artifacts,
      placements: [],
      launchConfigs: [],
    });
    // Linger from a previous render in this file can only be stale module
    // state; nothing should persist across tests, but the distributions map
    // is keyed per project and resetWorkspace() clears it. (Defensive: also
    // clear via a fresh setState with an explicit distributions key.)
    useWorkspaceStore.setState({ distributions: {} });
    vi.spyOn(transfersApi, "listBatches").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getProjectDistribution").mockResolvedValue(
      makeDistribution({ items }),
    );
  }

  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("renders matrix group header rows in code → dataset → model order with counts", async () => {
    setupWorkspace(["d1", "c1", "m1", "c2", "d2", "m2"], []);
    render(<ProjectDetail projectId="p1" />);
    const matrix = await screen.findByRole("table");
    const headers = within(matrix)
      .getAllByText(/ · /)
      .map((node) => node.textContent);
    expect(headers).toEqual(["code · 2", "dataset · 2", "model · 2"]);
    // Within-group order follows project artifact_ids (d1 before d2, etc.);
    // rows are grouped by kind, never re-sorted.
    const rows = within(matrix).getAllByRole("row");
    const rowLabels = rows.map((row) => row.textContent ?? "");
    expect(rowLabels.findIndex((text) => text.includes("probe-scaffold"))).toBeLessThan(
      rowLabels.findIndex((text) => text.includes("eval-harness")),
    );
    expect(rowLabels.findIndex((text) => text.includes("corpus-alpha"))).toBeLessThan(
      rowLabels.findIndex((text) => text.includes("corpus-beta")),
    );
    expect(rowLabels.findIndex((text) => text.includes("mit-b4-prima"))).toBeLessThan(
      rowLabels.findIndex((text) => text.includes("tiny-seg")),
    );
  });

  it("omits the group header for kinds with zero artifacts", async () => {
    setupWorkspace(["c1", "c2"], []);
    render(<ProjectDetail projectId="p1" />);
    const matrix = await screen.findByRole("table");
    const headers = within(matrix)
      .getAllByText(/ · /)
      .map((node) => node.textContent);
    expect(headers).toEqual(["code · 2"]);
  });

  it("renders textual states per cell: verified, missing, unavailable, syncing", async () => {
    setupWorkspace(["c1", "d1", "m1", "m2"], [
      makeDistributionItem({
        artifact_id: "c1",
        server_id: "srv-a",
        state: "verified",
      }),
      makeDistributionItem({
        artifact_id: "d1",
        server_id: "srv-a",
        state: "missing",
      }),
      makeDistributionItem({
        artifact_id: "m1",
        server_id: "srv-a",
        state: "unavailable",
      }),
      makeDistributionItem({
        artifact_id: "m2",
        server_id: "srv-a",
        state: "syncing",
      }),
      // A declared fallback: no item, but a placement exists on srv-b.
    ]);
    // Distribution items reference placements; add matching rows so the
    // snapshot states win the cell rendering (fallback path unchanged).
    useWorkspaceStore.setState({
      placements: [
        makePlacement({ artifact_id: "c1", server_id: "srv-b" }),
        makePlacement({ placement_id: "pl-d1", artifact_id: "d1", server_id: "srv-a" }),
        makePlacement({ placement_id: "pl-m1", artifact_id: "m1", server_id: "srv-a" }),
        makePlacement({ placement_id: "pl-m2", artifact_id: "m2", server_id: "srv-a" }),
      ],
    });
    render(<ProjectDetail projectId="p1" />);
    await screen.findByRole("table");
    // verified word
    expect(
      screen.getByLabelText("probe-scaffold / lab-4090 / verified"),
    ).toBeTruthy();
    // missing word
    expect(screen.getByLabelText("corpus-alpha / lab-4090 / missing")).toBeTruthy();
    // unavailable word
    expect(
      screen.getByLabelText("mit-b4-prima-full--ablation-feature-router / lab-4090 / unavailable"),
    ).toBeTruthy();
    // syncing word
    expect(screen.getByLabelText("tiny-seg:v1 / lab-4090 / syncing")).toBeTruthy();
    // declared fallback (placement, no snapshot item)
    expect(screen.getByLabelText("probe-scaffold / one4090 / declared")).toBeTruthy();
    // undeclared: no lozenge, bare dash cell only
    const undeclared = screen.getByLabelText(
      "corpus-alpha / one4090 / not placed",
    );
    expect(undeclared.querySelector(".ws-matrix__state")).toBeNull();
  });

  it("exposes the full artifact label via title on the artifact cell span", async () => {
    setupWorkspace(["m1"], []);
    render(<ProjectDetail projectId="p1" />);
    await screen.findByRole("table");
    const span = screen.getByTitle(
      "mit-b4-prima-full--ablation-feature-router",
    );
    expect(span.textContent).toBe("mit-b4-prima-full--ablation-feature-router");
  });

  it("groups PlacementTable rows by kind sections and one container per artifact", () => {
    setupWorkspace(["c1", "m1"], []);
    useWorkspaceStore.setState({
      placements: [
        makePlacement({ placement_id: "pl-1", artifact_id: "c1", server_id: "srv-a" }),
        makePlacement({ placement_id: "pl-2", artifact_id: "c1", server_id: "srv-b" }),
        makePlacement({ placement_id: "pl-3", artifact_id: "m1", server_id: "srv-a" }),
      ],
    });
    render(
      <PlacementTable
        artifacts={artifacts.filter((a) => ["c1", "m1"].includes(a.artifact_id))}
        placements={useWorkspaceStore.getState().placements}
        onEdit={() => undefined}
        onRemove={() => undefined}
        onSync={() => undefined}
      />,
    );

    const sectionHeads = screen
      .getAllByText(/ · /)
      .map((node) => node.textContent);
    expect(sectionHeads).toEqual(["code · 1", "model · 1"]);

    const groups = document.querySelectorAll(".ws-artifact-group");
    expect(groups).toHaveLength(2);
    const codeGroup = Array.from(groups).find((group) =>
      group.textContent?.includes("probe-scaffold"),
    );
    expect(codeGroup).toBeDefined();
    if (codeGroup !== undefined) {
      const groupScope = within(codeGroup as HTMLElement);
      expect(groupScope.getByText("lab-4090")).toBeTruthy();
      expect(groupScope.getByText("one4090")).toBeTruthy();
      expect(groupScope.getByText("2 placements")).toBeTruthy();
      // Inspect action and per-row menu still present.
      expect(groupScope.getAllByRole("button", { name: "Inspect" })).toHaveLength(2);
      expect(
        groupScope.getAllByRole("button", { name: /Placement actions: / }),
      ).toHaveLength(2);
    }
    // The kind word lives only in section headers, never in group heads.
    const codeGroupHead = codeGroup?.querySelector(".ws-artifact-group__head");
    expect(codeGroupHead?.textContent).not.toContain("code");
  });

  it("keeps the noPlacements hint inside the artifact group", () => {
    setupWorkspace(["d1"], []);
    render(
      <PlacementTable
        artifacts={[artifacts.find((a) => a.artifact_id === "d1")!]}
        placements={[]}
        onEdit={() => undefined}
        onRemove={() => undefined}
      />,
    );
    const group = document.querySelector(".ws-artifact-group");
    expect(group?.textContent).toContain("No placements declared");
  });
});

describe("workspace i18n parity", () => {
  it("zh mirrors every en workspace key (Dict-enforced at compile time too)", () => {
    expect(Object.keys(zh.workspace).sort()).toEqual(Object.keys(en.workspace).sort());
    for (const [key, value] of Object.entries(en.workspace)) {
      expect(typeof zh.workspace[key as keyof typeof en.workspace]).toBe("string");
      expect(typeof value).toBe("string");
    }
    expect(zh.workspace.title).toBe("工作区");
    expect(en.workspace.syncComingSoon).toBe("Transfer is coming in the next release");
    // Exclude-hint wording: entry root itself is never excluded.
    expect(en.workspace.syncExcludesHint).toContain("the transfer root itself is never excluded");
    expect(zh.workspace.syncExcludesHint).toContain("条目根目录本身不受影响");
  });

  it("renders catalog rows and the disabled sync-to menu item", async () => {
    vi.spyOn(workspaceApi, "listProjects").mockResolvedValue([makeProject()]);
    vi.spyOn(workspaceApi, "listArtifacts").mockResolvedValue([makeArtifact()]);
    vi.spyOn(workspaceApi, "listPlacements").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "listLaunchConfigs").mockResolvedValue([]);
    vi.spyOn(workspaceApi, "getServerRoots").mockResolvedValue({
      project_root: null,
      dataset_root: null,
      model_root: null,
      output_root: null,
    });
    render(<WorkspacePage />);

    expect(await screen.findByText("dinov2-linear")).toBeTruthy();

    // Datasets tab: the artifact row's ⋯ menu carries an enabled 同步到…
    // entry (Phase 2); opening it prefill-intents the Transfer Center dialog
    // and navigates there. (In tests the console store's hashchange wiring is
    // not active, so the route is driven directly.)
    fireEvent.click(screen.getByRole("button", { name: "Datasets" }));
    useConsoleStore.setState({ route: parseHash("#/workspace/datasets") });
    fireEvent.click(
      await screen.findByRole("button", { name: "Artifact actions: DINOv2" }),
    );
    const sync = await screen.findByRole("menuitem", { name: "Sync to…" });
    expect((sync as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(sync);
    expect(useTransferStore.getState().dialog.open).toBe(true);
    expect(useTransferStore.getState().dialog.prefill).toEqual({ artifactId: "a1" });
    expect(window.location.hash).toBe("#/transfers");
    useTransferStore.getState().closeNewTransfer();
    useTransferStore.getState().stopPolling();
  });
});

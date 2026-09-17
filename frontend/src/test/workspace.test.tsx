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
import { createWorkspaceStore, useWorkspaceStore } from "../store/workspaceStore";
import { useConsoleStore } from "../store/consoleStore";
import { useTransferStore } from "../store/transferStore";
import { en } from "../i18n/en";
import { zh } from "../i18n/zh";
import type { ServerRecord } from "../types/models";
import type {
  ArtifactRecord,
  LaunchConfigRecord,
  PlacementRecord,
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
  });

  afterEach(() => {
    resetWorkspace();
    resetConsole();
    vi.restoreAllMocks();
  });

  it("renders registry servers as columns and placements as ✓ cells", () => {
    render(<ProjectDetail projectId="p1" />);
    const matrix = screen.getByRole("table");
    expect(within(matrix).getByText("lab-4090")).toBeTruthy();
    expect(within(matrix).getByText("one4090")).toBeTruthy();
    expect(within(matrix).getByText("DINOv2:b")).toBeTruthy();
    // 4 cells: a1 on srv-a present; the other three are missing
    expect(screen.getAllByLabelText("present")).toHaveLength(1);
    expect(screen.getAllByLabelText("missing")).toHaveLength(3);
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

describe("workspace i18n parity", () => {
  it("zh mirrors every en workspace key (Dict-enforced at compile time too)", () => {
    expect(Object.keys(zh.workspace).sort()).toEqual(Object.keys(en.workspace).sort());
    for (const [key, value] of Object.entries(en.workspace)) {
      expect(typeof zh.workspace[key as keyof typeof en.workspace]).toBe("string");
      expect(typeof value).toBe("string");
    }
    expect(zh.workspace.title).toBe("工作区");
    expect(en.workspace.syncComingSoon).toBe("Transfer is coming in the next release");
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

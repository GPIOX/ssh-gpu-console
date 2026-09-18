/**
 * NewTransferDialog field coupling: switching artifact or source placement
 * re-aims the target server — an artifact with project peers (servers hosting
 * any placement of any artifact of a project containing it) prefers those
 * peers over a non-peer target, while peer-less artifacts keep the older
 * rule (only unset or source-colliding targets move). Switching artifacts
 * also drops a manual target path so the suggestion regenerates. All server,
 * artifact, and project names below are synthetic fixtures.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { transfersApi } from "../../services/transfersApi";
import { useConsoleStore } from "../../store/consoleStore";
import { useTransferStore } from "../../store/transferStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import type { TransferPlan } from "../../types/transfers";
import type { ServerRecord } from "../../types/models";
import type {
  ArtifactRecord,
  PlacementRecord,
  ProjectRecord,
  ServerRoots,
} from "../../types/workspace";
import { NewTransferDialog } from "./NewTransferDialog";

const NOW = "2026-09-17T12:00:00Z";

// Synthetic registry: srv-a = "Server A", srv-b = "Server B", srv-c = "Server C".
const servers: ServerRecord[] = [
  { server_id: "srv-a", display_name: "Server A", ssh_host: "host-a", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-b", display_name: "Server B", ssh_host: "host-b", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-c", display_name: "Server C", ssh_host: "host-c", username: null, port: 22, tags: [], enabled: true },
];

const roots: ServerRoots = {
  project_root: "/proj",
  dataset_root: "/data",
  model_root: "/models",
  output_root: null,
};

function makeArtifact(overrides: Partial<ArtifactRecord> = {}): ArtifactRecord {
  return {
    artifact_id: "art-1",
    kind: "dataset",
    name: "artifact-one",
    version: null,
    description: "",
    immutable: true,
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

// artifact-empty has no placements (the broken seeding scenario);
// artifact-one and artifact-two are both sourced on Server A.
const artEmpty = makeArtifact({ artifact_id: "art-empty", name: "artifact-empty" });
const artOne = makeArtifact();
const artTwo = makeArtifact({ artifact_id: "art-2", name: "artifact-two" });

function makePlacement(overrides: Partial<PlacementRecord> = {}): PlacementRecord {
  return {
    placement_id: "pl-1",
    artifact_id: "art-1",
    server_id: "srv-a",
    remote_path: "/data/artifact-one",
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

function makeProject(overrides: Partial<ProjectRecord> = {}): ProjectRecord {
  return {
    project_id: "proj-1",
    name: "Project One",
    description: "",
    artifact_ids: [],
    launch_config_ids: [],
    tags: [],
    transfer_excludes: [],
    created_at: NOW,
    updated_at: NOW,
    ...overrides,
  };
}

const plOne = makePlacement();
const plTwo = makePlacement({ placement_id: "pl-2", artifact_id: "art-2", remote_path: "/data/artifact-two" });
const plOneOnB = makePlacement({ placement_id: "pl-1b", server_id: "srv-b", remote_path: "/mirror/artifact-one" });
const plOneOnC = makePlacement({ placement_id: "pl-1c", server_id: "srv-c", remote_path: "/relay/artifact-one" });
const plTwoOnB = makePlacement({ placement_id: "pl-2b", artifact_id: "art-2", server_id: "srv-b", remote_path: "/mirror/artifact-two" });
const plTwoOnC = makePlacement({ placement_id: "pl-2c", artifact_id: "art-2", server_id: "srv-c", remote_path: "/relay/artifact-two" });

const planFixture: TransferPlan = {
  artifact_id: "art-1",
  source_server_id: "srv-a",
  source_path: "/data/artifact-one",
  target_server_id: "srv-b",
  target_path: "/data/artifact-one",
  strategy_requested: "auto",
  strategy_available: { direct_rsync: true, local_relay: true },
  strategy_selected: "direct_rsync",
  reason: "",
  source_exists: true,
  source_size_b: null,
  excludes: [],
  target_free_b: null,
  space_warning: "",
};

function resetStores(): void {
  useTransferStore.setState({
    jobs: [],
    loading: false,
    error: null,
    polling: false,
    batches: [],
    batchesLoading: false,
    batchesError: null,
    dialog: { open: false, prefill: {} },
  });
  useTransferStore.getState().stopPolling();
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
    serverRoots: {},
    rootsLoading: {},
    rootsErrors: {},
  });
  useConsoleStore.setState({ servers: [], statuses: {} });
}

describe("NewTransferDialog field coupling", () => {
  beforeEach(() => {
    // Plan is an SSH preflight — always mocked so no network happens.
    vi.spyOn(transfersApi, "plan").mockResolvedValue(planFixture);
  });

  afterEach(() => {
    cleanup();
    resetStores();
    vi.restoreAllMocks();
  });

  it("T1: seeded with a placement-less artifact, picking a Server-A-sourced artifact re-aims the target away from Server A", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artEmpty, artOne],
      placements: [plOne],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    // No prefill: seeds artifact-empty (no placements), target = first enabled server.
    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).toBe("srv-a"); // "Server A"

    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });

    expect((screen.getByLabelText("Source placement") as HTMLSelectElement).value).toBe("pl-1");
    expect(target.value).not.toBe("srv-a");
    expect(target.value).toBe("srv-b"); // "Server B"
  });

  it("T2: keeps a deliberate target choice when switching to another artifact with the same source", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artEmpty, artOne, artTwo],
      placements: [plOne, plTwo],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).toBe("srv-a");

    // Selecting the Server-A-sourced artifact flips the colliding target to Server B.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect(target.value).toBe("srv-b");

    // Deliberate target choice: Server C.
    fireEvent.change(target, { target: { value: "srv-c" } });
    expect(target.value).toBe("srv-c");

    // Another artifact still sourced on Server A must not clobber that choice.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-2" } });
    expect(target.value).toBe("srv-c");
  });

  it("T3: switching artifact replaces a manual path with the new artifact's suggested path", async () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artEmpty, artOne, artTwo],
      placements: [plOne, plTwo],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    render(<NewTransferDialog open />);

    // artifact-one on Server A flips the target to Server B; the dataset root
    // suggests /data/artifact-one.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect((screen.getByLabelText("Target server") as HTMLSelectElement).value).toBe("srv-b");
    await screen.findByDisplayValue("/data/artifact-one");

    // Manual edit wins for artifact-one...
    fireEvent.change(screen.getByLabelText("Target path"), {
      target: { value: "/custom/path" },
    });
    expect(screen.getByDisplayValue("/custom/path")).toBeTruthy();

    // ...but switching artifacts drops the stale manual path and regenerates.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-2" } });
    expect(await screen.findByDisplayValue("/data/artifact-two")).toBeTruthy();
    expect(screen.queryByDisplayValue("/custom/path")).toBeNull();
  });

  it("T4: moving the source onto the manually chosen target server flips the target away", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artOne],
      placements: [plOne, plOneOnC],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "art-1", sourcePlacementId: "pl-1" } },
    });

    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).toBe("srv-b"); // seeded away from source srv-a

    // Manual choice: Server C (still != source).
    fireEvent.change(target, { target: { value: "srv-c" } });
    expect(target.value).toBe("srv-c");

    // Source placement moves to Server C == current target → auto re-aim.
    fireEvent.change(screen.getByLabelText("Source placement"), {
      target: { value: "pl-1c" },
    });
    expect(target.value).not.toBe("srv-c");
    expect(target.value).toBe("srv-a"); // "Server A"
  });

  it("T5: project peers flip the target to the peer server even over a valid manual choice", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      // Project One spans both artifacts: art-1 lives on Server A (source),
      // art-2 lives on Server B — Server B is the project peer of Server A.
      projects: [makeProject({ artifact_ids: ["art-1", "art-2"] })],
      artifacts: [artEmpty, artOne, artTwo],
      placements: [plOne, plTwoOnB],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    // Seeded with the placement-less artifact: no peers, first enabled server.
    expect(target.value).toBe("srv-a");

    // Deliberate (still-valid) target choice: Server C.
    fireEvent.change(target, { target: { value: "srv-c" } });
    expect(target.value).toBe("srv-c");

    // Selecting the peer-context artifact overrides the valid non-peer choice.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect((screen.getByLabelText("Source placement") as HTMLSelectElement).value).toBe("pl-1");
    expect(target.value).toBe("srv-b"); // project peer of the source
  });

  it("T6: peers only on the source server (or none) keep a valid manual target", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      // Project One spans both artifacts but every placement sits on Server A,
      // so the peer set minus the source is empty.
      projects: [makeProject({ artifact_ids: ["art-1", "art-2"] })],
      artifacts: [artEmpty, artOne, artTwo],
      placements: [plOne, plTwo],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).toBe("srv-a");

    fireEvent.change(target, { target: { value: "srv-c" } });

    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect(target.value).toBe("srv-c"); // no peers → collision rule keeps Server C

    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-2" } });
    expect(target.value).toBe("srv-c");
  });

  it("T7: multi-peer artifact keeps a peer target, else takes the first peer in server order", () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      // Project One references artifact-one, deployed on Servers A, B, and C.
      projects: [makeProject({ artifact_ids: ["art-1"] })],
      artifacts: [artEmpty, artOne, artTwo],
      placements: [plOne, plOneOnB, plOneOnC, plTwo],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });

    render(<NewTransferDialog open />);
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).toBe("srv-a");

    // Peers = {srv-b, srv-c}; the source-colliding target becomes the first
    // peer in servers-list order.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect(target.value).toBe("srv-b");

    // Deliberate peer choice: Server C.
    fireEvent.change(target, { target: { value: "srv-c" } });

    // Via peer-less artifact-2 (kept by the collision rule) and back: Server C
    // is itself a peer, so the artifact switch keeps it.
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-2" } });
    expect(target.value).toBe("srv-c");
    fireEvent.change(screen.getByLabelText("Artifact"), { target: { value: "art-1" } });
    expect(target.value).toBe("srv-c");
  });

  it("T8 regression: prefill still seeds artifact, source, peer-suggested target, and path", async () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      // Project One spans both artifacts; art-2 lives on Server C, making it
      // the project peer of the source Server A (beats the plain first-enabled
      // fallback Server B).
      projects: [makeProject({ artifact_ids: ["art-1", "art-2"] })],
      artifacts: [artOne],
      placements: [plOne, plTwoOnC],
      serverRoots: { "srv-a": roots, "srv-b": roots, "srv-c": roots },
    });
    useTransferStore.setState({
      dialog: { open: true, prefill: { artifactId: "art-1", sourcePlacementId: "pl-1" } },
    });

    render(<NewTransferDialog open />);

    expect((screen.getByLabelText("Artifact") as HTMLSelectElement).value).toBe("art-1");
    expect((screen.getByLabelText("Source placement") as HTMLSelectElement).value).toBe("pl-1");
    const target = screen.getByLabelText("Target server") as HTMLSelectElement;
    expect(target.value).not.toBe("srv-a");
    expect(target.value).toBe("srv-c"); // peer suggestion wins at seed time
    // The suggested path proves target != source end to end.
    await screen.findByDisplayValue("/data/artifact-one");
  });
});

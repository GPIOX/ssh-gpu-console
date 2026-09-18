/**
 * Phase 4.2D: when the transfer plan reports direct rsync unavailable, the
 * plan area gains a humanized reason lead (raw reason stays as secondary
 * mono detail), a 配置直连 button, and the DirectAuthSetupDialog for the
 * current (source → target) pair. All names are synthetic fixtures.
 */

import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { transfersApi } from "../services/transfersApi";
import { api } from "../services/api";
import { useConsoleStore } from "../store/consoleStore";
import { useTransferStore } from "../store/transferStore";
import { useWorkspaceStore } from "../store/workspaceStore";
import type { TransferPlan } from "../types/transfers";
import type { ServerRecord } from "../types/models";
import type { ArtifactRecord, PlacementRecord, ServerRoots } from "../types/workspace";
import { NewTransferDialog } from "../features/transfers/NewTransferDialog";

const NOW = "2026-09-17T12:00:00Z";

const servers: ServerRecord[] = [
  { server_id: "srv-src", display_name: "Alpha Box", ssh_host: "alpha", username: null, port: 22, tags: [], enabled: true },
  { server_id: "srv-dst", display_name: "Beta Box", ssh_host: "beta", username: null, port: 22, tags: [], enabled: true },
];

const artifact: ArtifactRecord = {
  artifact_id: "art-9",
  kind: "dataset",
  name: "synthetic-set",
  version: null,
  description: "",
  immutable: true,
  created_at: NOW,
  updated_at: NOW,
};

const placement: PlacementRecord = {
  placement_id: "pl-9",
  artifact_id: "art-9",
  server_id: "srv-src",
  remote_path: "/data/synthetic-set",
  created_at: NOW,
  updated_at: NOW,
};

const roots: ServerRoots = {
  project_root: "/proj",
  dataset_root: "/data",
  model_root: "/models",
  output_root: null,
};

const UNAVAILABLE_PLAN: TransferPlan = {
  artifact_id: "art-9",
  source_server_id: "srv-src",
  source_path: "/data/synthetic-set",
  target_server_id: "srv-dst",
  target_path: "/data/synthetic-set",
  strategy_requested: "auto",
  strategy_available: { direct_rsync: false, local_relay: true },
  strategy_selected: "local_relay",
  reason: "authentication_failed",
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

let planSpy: MockInstance<(requestBody: Parameters<typeof transfersApi.plan>[0]) => Promise<TransferPlan>>;

beforeEach(() => {
  planSpy = vi.spyOn(transfersApi, "plan").mockResolvedValue(UNAVAILABLE_PLAN);
  vi.spyOn(api, "getDirectAuth").mockResolvedValue({
    server_id: "srv-src",
    pairs: [
      {
        source_server_id: "srv-src",
        target_server_id: "srv-dst",
        configured: false,
        method: null,
        available: null,
        reason: null,
        checked_at: null,
      },
    ],
  });
});

afterEach(() => {
  cleanup();
  resetStores();
  vi.restoreAllMocks();
});

describe("transfer dialog: unavailable direct rsync", () => {
  it("shows the humanized reason lead, the raw reason detail, and the 配置直连 CTA", async () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artifact],
      placements: [placement],
      serverRoots: { "srv-src": roots, "srv-dst": roots },
    });

    render(<NewTransferDialog open />);
    expect(
      await screen.findByText("Reason: the source server cannot authenticate to the target server"),
    ).toBeTruthy();
    expect(screen.getByText("authentication_failed")).toBeTruthy(); // secondary mono detail
    expect(screen.getByRole("button", { name: "Configure direct" })).toBeTruthy();
  });

  it("opens the setup dialog for the current source → target pair only on click", async () => {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artifact],
      placements: [placement],
      serverRoots: { "srv-src": roots, "srv-dst": roots },
    });

    render(<NewTransferDialog open />);
    await screen.findByText("Reason: the source server cannot authenticate to the target server");
    // No metadata GET and no check before the explicit click.
    expect(api.getDirectAuth).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Configure direct" }));

    // Dialog titles the exact pair; the pair row is wired source → target.
    expect(
      await screen.findByText("Direct transfer setup: Alpha Box → Beta Box"),
    ).toBeTruthy();
    expect(api.getDirectAuth).toHaveBeenCalledWith("srv-src");
    const row = await screen.findByText("Alpha Box", { selector: ".da-pair__name" });
    const pair = row.closest(".da-pair");
    expect(pair?.getAttribute("data-source")).toBe("srv-src");
    expect(pair?.getAttribute("data-target")).toBe("srv-dst");
    expect(screen.getByRole("button", { name: "Check direct" })).toBeTruthy();
  });

  it("after a successful setup offers 重新规划 which re-runs the plan request", async () => {
    const checkSpy = vi
      .spyOn(api, "checkDirectAuth")
      .mockResolvedValue({
        target_server_id: "srv-dst",
        configured: false,
        method: "native",
        available: true,
        reason: null,
        checked_at: "2026-09-18T00:00:00Z",
      });
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artifact],
      placements: [placement],
      serverRoots: { "srv-src": roots, "srv-dst": roots },
    });

    render(<NewTransferDialog open />);
    await screen.findByText("Reason: the source server cannot authenticate to the target server");
    const planCallsBefore = planSpy.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "Configure direct" }));
    fireEvent.click(await screen.findByRole("button", { name: "Check direct" }));
    await screen.findByText("✓ Ready for direct authentication — no setup needed");
    expect(checkSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Re-plan" }));

    await waitFor(() => {
      expect(planSpy.mock.calls.length).toBeGreaterThan(planCallsBefore);
    });
  });
});

describe("setup dialog pair seeding (pairs shape)", () => {
  function openSetup(): void {
    useConsoleStore.setState({ servers, statuses: {} });
    useWorkspaceStore.setState({
      artifacts: [artifact],
      placements: [placement],
      serverRoots: { "srv-src": roots, "srv-dst": roots },
    });
    render(<NewTransferDialog open />);
  }

  it("seeds from the exact (source → target) pair when both directions are listed", async () => {
    openSetup();
    await screen.findByText("Reason: the source server cannot authenticate to the target server");
    // Wrong-direction pair (srv-dst → srv-src) listed first; only the exact
    // (srv-src → srv-dst) pair may seed the row.
    vi.spyOn(api, "getDirectAuth").mockResolvedValue({
      server_id: "srv-src",
      pairs: [
        {
          source_server_id: "srv-dst",
          target_server_id: "srv-src",
          configured: false,
          method: null,
          available: null,
          reason: null,
          checked_at: null,
        },
        {
          source_server_id: "srv-src",
          target_server_id: "srv-dst",
          configured: true,
          method: "sgc_key",
          available: true,
          reason: null,
          checked_at: NOW,
        },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: "Configure direct" }));

    expect(await screen.findByText("✓ Dedicated key in place")).toBeTruthy();
    expect(api.getDirectAuth).toHaveBeenCalledWith("srv-src");
    expect(api.getDirectAuth).toHaveBeenCalledTimes(1); // exact hit — no fallback
  });

  it("falls back to the target id's listing when the source listing lacks the pair", async () => {
    openSetup();
    await screen.findByText("Reason: the source server cannot authenticate to the target server");
    vi.spyOn(api, "getDirectAuth")
      .mockResolvedValueOnce({ server_id: "srv-src", pairs: [] })
      .mockResolvedValue({
        server_id: "srv-dst",
        pairs: [
          {
            source_server_id: "srv-src",
            target_server_id: "srv-dst",
            configured: true,
            method: "sgc_key",
            available: true,
            reason: null,
            checked_at: NOW,
          },
        ],
      });

    fireEvent.click(screen.getByRole("button", { name: "Configure direct" }));

    expect(await screen.findByText("✓ Dedicated key in place")).toBeTruthy();
    expect(api.getDirectAuth).toHaveBeenCalledWith("srv-src");
    expect(api.getDirectAuth).toHaveBeenCalledWith("srv-dst");
  });
});

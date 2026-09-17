/**
 * Detail-section visibility preference (Feature 1): the preference module's
 * semantics (localStorage `sgc.sections`, defensive load), the tab strip
 * filtering in ServerDetailTabs, and the ServerPage deep-link guard that
 * rewrites #/server/{id}/{hiddenTab} back to Overview.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { ServerPage } from "../features/server/ServerPage";
import { ServerDetailTabs } from "../shell/ServerDetailTabs";
import { fixtureServers, fixtureSnapshots } from "../data/fixtures";
import { setLocale } from "../i18n";
import { useConsoleStore } from "../store/consoleStore";
import {
  getHiddenSections,
  isSectionHidden,
  TOGGLEABLE_SECTIONS,
  toggleSection,
} from "../utils/sections";
import type { ServerTab } from "../shell/routes";

const LAB = "srv-lab-4090";

function makeAllVisible(): void {
  for (const tab of [...getHiddenSections()]) toggleSection(tab);
}

function seedServerRoute(tab: ServerTab): void {
  const snapshot = fixtureSnapshots[LAB];
  useConsoleStore.setState({
    route: { name: "server", serverId: LAB, tab },
    selectedServerId: LAB,
    selectedTab: tab,
    servers: fixtureServers,
    snapshots: { [LAB]: snapshot },
    snapshotErrors: {},
    statuses: { [LAB]: snapshot.status },
    history: new Map(),
  });
}

beforeEach(() => {
  setLocale("en");
  makeAllVisible();
  window.location.hash = ""; // jsdom keeps the hash across tests in a file
});

afterEach(() => {
  makeAllVisible();
});

describe("sections preference module", () => {
  it("defaults to all sections visible", () => {
    expect(getHiddenSections()).toEqual([]);
    for (const tab of TOGGLEABLE_SECTIONS) {
      expect(isSectionHidden(tab)).toBe(false);
    }
  });

  it("toggles a section off and on, persisting the hidden ids as a JSON array", () => {
    toggleSection("network");
    expect(getHiddenSections()).toEqual(["network"]);
    expect(localStorage.getItem("sgc.sections")).toBe(JSON.stringify(["network"]));

    toggleSection("network");
    expect(getHiddenSections()).toEqual([]);
    expect(localStorage.getItem("sgc.sections")).toBe("[]");
  });

  it("never hides overview or gpus", () => {
    act(() => toggleSection("overview"));
    act(() => toggleSection("gpus"));
    expect(getHiddenSections()).toEqual([]);
    expect(isSectionHidden("overview")).toBe(false);
    expect(isSectionHidden("gpus")).toBe(false);
  });

  it("ignores malformed or out-of-vocabulary persisted payloads", async () => {
    localStorage.setItem(
      "sgc.sections",
      JSON.stringify(["overview", "gpus", "bogus", 42, "network"]),
    );
    vi.resetModules();
    const filtered = await import("../utils/sections");
    expect(filtered.getHiddenSections()).toEqual(["network"]);

    localStorage.setItem("sgc.sections", "not-json");
    vi.resetModules();
    const corrupt = await import("../utils/sections");
    expect(corrupt.getHiddenSections()).toEqual([]);
  });
});

describe("ServerDetailTabs — hidden sections are filtered", () => {
  it("shows every tab when nothing is hidden", () => {
    seedServerRoute("overview");
    render(<ServerDetailTabs />);
    for (const label of ["Overview", "GPUs", "Processes", "System", "Storage", "Network"]) {
      expect(screen.getByRole("button", { name: label })).toBeTruthy();
    }
  });

  it("drops toggled-off sections while overview/gpus always stay", () => {
    seedServerRoute("overview");
    act(() => toggleSection("processes"));
    render(<ServerDetailTabs />);

    expect(screen.queryByRole("button", { name: "Processes" })).toBeNull();
    expect(screen.getByRole("button", { name: "Overview" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "GPUs" })).toBeTruthy();

    act(() => toggleSection("processes"));
    expect(screen.getByRole("button", { name: "Processes" })).toBeTruthy();
  });
});

describe("ServerPage — deep link to a hidden section", () => {
  it("renders Overview instead of the hidden section and rewrites the route once", () => {
    seedServerRoute("network");
    act(() => toggleSection("network"));
    render(<ServerPage serverId={LAB} />);

    // Overview content renders immediately (guard is not just a route rewrite)
    expect(screen.getByText("Top processes")).toBeTruthy();

    // The route was rewritten once to the Overview hash, never back to network
    expect(window.location.hash).toBe(`#/server/${LAB}/overview`);
    useConsoleStore.getState().applyHash();
    const route = useConsoleStore.getState().route;
    expect(route).toEqual({ name: "server", serverId: LAB, tab: "overview" });
    expect(window.location.hash).toBe(`#/server/${LAB}/overview`);
  });

  it("keeps a visible deep link untouched", () => {
    seedServerRoute("storage");
    render(<ServerPage serverId={LAB} />);
    expect(screen.getByText("/data")).toBeTruthy();
    expect(window.location.hash).toBe("");
  });
});

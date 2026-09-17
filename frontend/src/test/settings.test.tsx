/**
 * Settings page + add/edit server dialog: alias import fills host/user/port,
 * manual entry, submit → create → inline test flow, host-key TOFU panel
 * (explicit trust only), failure-taxonomy help, edit prefill, named remove
 * confirmation, and the one-shot backend health probe.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { SettingsPage } from "../features/settings/SettingsPage";
import { api } from "../services/api";
import { useConsoleStore } from "../store/consoleStore";
import { getHiddenSections, toggleSection } from "../utils/sections";
import type {
  AliasEntry,
  ConnectionTestResult,
  FleetSummary,
  ServerRecord,
} from "../types/models";

const ALIASES: AliasEntry[] = [
  { alias: "lab-4090", host: "192.168.1.40", user: "demo", port: 1111 },
  { alias: "dgx", host: "dgx.local", user: null, port: null },
];

const OK_RESULT: ConnectionTestResult = {
  ok: true,
  status: "online",
  detail: "handshake ok",
  latency_ms: 34,
  pending_host_key: null,
};

const HOST_KEY_RESULT: ConnectionTestResult = {
  ok: false,
  status: "host_key_error",
  detail: "host key verification failed",
  latency_ms: 12,
  pending_host_key: {
    host: "lab-4090",
    port: 1111,
    key_type: "ssh-ed25519",
    fingerprint: "SHA256:AAAA1234",
  },
};

function makeRecord(
  overrides: Partial<ServerRecord> & Pick<ServerRecord, "server_id" | "display_name">,
): ServerRecord {
  return {
    ssh_host: "10.0.0.8",
    username: "demo",
    port: 22,
    tags: [],
    enabled: true,
    ...overrides,
  };
}

const EMPTY_FLEET: FleetSummary = { generated_at: "2026-01-01T00:00:00Z", servers: [] };

function resetStore(): void {
  useConsoleStore.setState({
    servers: [],
    serversLoading: false,
    serversError: null,
    statuses: {},
    fleetSummary: null,
    fleetError: null,
  });
}

async function openAddDialog(): Promise<HTMLElement> {
  // With an empty registry the only "Add server" button lives in the empty state.
  fireEvent.click(screen.getByRole("button", { name: "Add server" }));
  const dialog = await screen.findByRole("dialog");
  await screen.findByText("From ssh config"); // form view settled
  return dialog;
}

beforeEach(() => {
  resetStore();
  vi.spyOn(api, "listServers").mockResolvedValue([]);
  vi.spyOn(api, "getFleet").mockResolvedValue(EMPTY_FLEET);
  vi.spyOn(api, "listSshAliases").mockResolvedValue(ALIASES);
  vi.spyOn(api, "getHealth").mockResolvedValue({
    status: "ok",
    scheduler_mode: "interactive",
    realtime_clients: 2,
  });
});

afterEach(() => {
  resetStore();
  vi.restoreAllMocks();
});

describe("settings registry table", () => {
  it("renders name, mono endpoint, tag chips, enabled chip, and status word", () => {
    useConsoleStore.setState({
      servers: [
        makeRecord({
          server_id: "srv-a",
          display_name: "alpha",
          tags: ["gpu", "lab"],
        }),
      ],
      statuses: { "srv-a": "offline" },
    });
    render(<SettingsPage />);

    expect(screen.getByText("alpha")).toBeTruthy();
    expect(screen.getByText("demo@10.0.0.8:22")).toBeTruthy();
    expect(screen.getByText("gpu")).toBeTruthy();
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.getByText("offline")).toBeTruthy();
  });

  it("shows an empty state with its own Add entry point when the registry is empty", () => {
    render(<SettingsPage />);
    expect(screen.getByText("No servers yet")).toBeTruthy();
    const addButtons = screen.getAllByRole("button", { name: "Add server" });
    expect(addButtons.length).toBeGreaterThanOrEqual(1);
  });

  it("probes backend health exactly once on open and renders the facts", async () => {
    render(<SettingsPage />);
    expect(await screen.findByText("scheduler interactive")).toBeTruthy();
    expect(screen.getByText("2 realtime clients")).toBeTruthy();
    expect(api.getHealth).toHaveBeenCalledTimes(1);
  });
});

describe("add server dialog: ssh config aliases", () => {
  it("selecting an alias fills ssh_host, username, and port", async () => {
    const createSpy = vi
      .spyOn(api, "createServer")
      .mockResolvedValue(makeRecord({ server_id: "srv-new", display_name: "Lab" }));
    vi.spyOn(api, "testConnection").mockResolvedValue(OK_RESULT);
    render(<SettingsPage />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    const select = await screen.findByLabelText("SSH config alias");
    fireEvent.change(select, { target: { value: "lab-4090" } });

    // The alias pre-fills username/port and annotates the resolved endpoint.
    expect((screen.getByLabelText("Username") as HTMLInputElement).value).toBe("demo");
    expect((screen.getByLabelText("Port") as HTMLInputElement).value).toBe("1111");
    expect(screen.getByText("demo@192.168.1.40:1111")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Lab" } });
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        display_name: "Lab",
        ssh_host: "lab-4090",
        username: "demo",
        port: 1111,
        tags: [],
        enabled: true,
      });
    });
  });

  it("shows skeleton loading, then an empty-alias path with manual fallback", async () => {
    vi.spyOn(api, "listSshAliases").mockImplementation(
      () => new Promise<AliasEntry[]>(() => undefined), // never resolves: loading state
    );
    render(<SettingsPage />);
    await openAddDialog();
    // The dialog portals to document.body — query there, not the render container.
    expect(document.querySelector(".settings-dialog__form .skeleton")).toBeTruthy();
  });

  it("shows an empty-alias path with a manual fallback once loaded empty", async () => {
    vi.spyOn(api, "listSshAliases").mockResolvedValue([]);
    render(<SettingsPage />);
    await openAddDialog();

    expect(screen.getByText("no aliases found — use manual entry")).toBeTruthy();
    fireEvent.click(screen.getByText("Switch to manual entry"));
    expect(screen.getByLabelText("Host")).toBeTruthy();
    expect(screen.queryByText("no aliases found — use manual entry")).toBeNull();
  });

  it("surfaces alias load failures with a retry that recovers", async () => {
    vi.spyOn(api, "listSshAliases")
      .mockRejectedValueOnce(new Error("ssh config unreadable"))
      .mockResolvedValueOnce(ALIASES);
    render(<SettingsPage />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    expect(await screen.findByText("Could not load ssh-config aliases")).toBeTruthy();
    expect(screen.getByText("ssh config unreadable")).toBeTruthy();

    fireEvent.click(screen.getByText("Retry"));
    expect(await screen.findByLabelText("SSH config alias")).toBeTruthy();
  });
});

describe("add server dialog: manual entry + validation", () => {
  it("submits manual host, optional username, parsed port, and comma tags", async () => {
    const createSpy = vi
      .spyOn(api, "createServer")
      .mockResolvedValue(makeRecord({ server_id: "srv-box", display_name: "Box" }));
    vi.spyOn(api, "testConnection").mockResolvedValue(OK_RESULT);
    render(<SettingsPage />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    await screen.findByText("From ssh config");
    fireEvent.click(screen.getByRole("button", { name: "Manual" }));

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Box" } });
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "10.1.2.3" } });
    fireEvent.change(screen.getByLabelText("Port (optional)"), { target: { value: "2222" } });
    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "gpu, lab" } });
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith({
        display_name: "Box",
        ssh_host: "10.1.2.3",
        username: null,
        port: 2222,
        tags: ["gpu", "lab"],
        enabled: true,
      });
    });
  });

  it("keeps the add button disabled on an empty name or host", async () => {
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));

    await screen.findByText("From ssh config");
    const submit = () =>
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" });
    expect((submit() as HTMLButtonElement).disabled).toBe(true); // nothing filled

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Lab" } });
    expect((submit() as HTMLButtonElement).disabled).toBe(true); // no host yet

    fireEvent.change(await screen.findByLabelText("SSH config alias"), {
      target: { value: "lab-4090" },
    });
    expect((submit() as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Manual" }));
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "" } });
    expect((submit() as HTMLButtonElement).disabled).toBe(true); // manual host cleared
  });
});

describe("add server dialog: test flow", () => {
  async function fillAndSubmit(testResult: ConnectionTestResult): Promise<void> {
    vi.spyOn(api, "createServer").mockResolvedValue(
      makeRecord({ server_id: "srv-new", display_name: "Lab", ssh_host: "lab-4090" }),
    );
    vi.spyOn(api, "testConnection").mockResolvedValue(testResult);
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.change(await screen.findByLabelText("SSH config alias"), {
      target: { value: "lab-4090" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Lab" } });
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );
  }

  it("creates first, then auto-runs the test and shows status word + latency", async () => {
    await fillAndSubmit(OK_RESULT);

    expect(await screen.findByText("34 ms")).toBeTruthy();
    expect(screen.getByText("connected", { selector: ".chip" })).toBeTruthy();
    expect(api.testConnection).toHaveBeenCalledWith("srv-new");
    expect(screen.getByText("Done")).toBeTruthy();
  });

  it("renders the host-key security panel and calls trust ONLY on explicit click", async () => {
    const trustSpy = vi.spyOn(api, "trustHostKey").mockResolvedValue(undefined);
    await fillAndSubmit(HOST_KEY_RESULT);

    expect(
      await screen.findByText(/authenticity of host lab-4090 can't be established/),
    ).toBeTruthy();
    expect(screen.getByText(/key type ssh-ed25519/)).toBeTruthy();
    expect(screen.getByText("SHA256:AAAA1234")).toBeTruthy();
    expect(
      screen.getByText("Verify this fingerprint out-of-band, then trust it to connect."),
    ).toBeTruthy();
    expect(trustSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Trust key and retry" }));
    await waitFor(() => {
      expect(trustSpy).toHaveBeenCalledWith("srv-new", HOST_KEY_RESULT.pending_host_key);
    });
  });

  it("re-runs the test after trusting and recovers to the success view", async () => {
    vi.spyOn(api, "trustHostKey").mockResolvedValue(undefined);
    vi.spyOn(api, "createServer").mockResolvedValue(
      makeRecord({ server_id: "srv-new", display_name: "Lab", ssh_host: "lab-4090" }),
    );
    vi.spyOn(api, "testConnection")
      .mockResolvedValueOnce(HOST_KEY_RESULT)
      .mockResolvedValueOnce(OK_RESULT);
    render(<SettingsPage />);

    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    fireEvent.change(await screen.findByLabelText("SSH config alias"), {
      target: { value: "lab-4090" },
    });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Lab" } });
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );

    fireEvent.click(await screen.findByRole("button", { name: "Trust key and retry" }));
    expect(await screen.findByText("34 ms")).toBeTruthy();
    expect(api.testConnection).toHaveBeenCalledTimes(2);
  });

  it("shows auth-specific help for authentication_failed and different help for timeout", async () => {
    await fillAndSubmit({
      ok: false,
      status: "authentication_failed",
      detail: "permission denied (publickey)",
      latency_ms: 40,
      pending_host_key: null,
    });
    expect(
      await screen.findByText(/SSH agent has a key the server accepts/),
    ).toBeTruthy();
    expect(screen.getByText("auth failed", { selector: ".chip" })).toBeTruthy();

    // Retry with a different failure — the help text follows the taxonomy.
    vi.spyOn(api, "testConnection").mockResolvedValue({
      ok: false,
      status: "timeout",
      detail: "no answer within 10s",
      latency_ms: null,
      pending_host_key: null,
    });
    fireEvent.click(screen.getByRole("button", { name: "Retry test" }));
    expect(await screen.findByText(/server is unreachable/)).toBeTruthy();
    expect(screen.queryByText(/SSH agent has a key the server accepts/)).toBeNull();
  });
});

describe("edit server dialog", () => {
  it("prefills every field, saves via patchServer, and offers a stored-record test", async () => {
    const patchSpy = vi
      .spyOn(api, "patchServer")
      .mockResolvedValue(makeRecord({ server_id: "srv-a", display_name: "alpha" }));
    useConsoleStore.setState({
      servers: [
        makeRecord({
          server_id: "srv-a",
          display_name: "alpha",
          tags: ["gpu", "lab"],
        }),
      ],
      statuses: {},
    });
    render(<SettingsPage />);

    fireEvent.click(screen.getByLabelText("Server registry: alpha"));
    fireEvent.click(screen.getByText("Edit"));
    await screen.findByRole("dialog");

    expect((screen.getByLabelText("Display name") as HTMLInputElement).value).toBe("alpha");
    expect((screen.getByLabelText("Host") as HTMLInputElement).value).toBe("10.0.0.8");
    expect((screen.getByLabelText("Username (optional)") as HTMLInputElement).value).toBe("demo");
    expect((screen.getByLabelText("Port (optional)") as HTMLInputElement).value).toBe("22");
    expect((screen.getByLabelText("Tags") as HTMLInputElement).value).toBe("gpu, lab");
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "alpha-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(
        "srv-a",
        expect.objectContaining({ display_name: "alpha-2", ssh_host: "10.0.0.8" }),
      );
    });
  });

  it("removes through a confirmation that names the server and endpoint", async () => {
    const deleteSpy = vi.spyOn(api, "deleteServer").mockResolvedValue(undefined);
    useConsoleStore.setState({
      servers: [makeRecord({ server_id: "srv-a", display_name: "alpha" })],
      statuses: {},
    });
    render(<SettingsPage />);

    fireEvent.click(screen.getByLabelText("Server registry: alpha"));
    fireEvent.click(screen.getByText("Remove"));

    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/Remove server/)).toBeTruthy();
    expect(within(dialog).getAllByText(/Remove alpha/).length).toBeGreaterThan(0);
    expect(within(dialog).getByText("demo@10.0.0.8:22")).toBeTruthy();

    fireEvent.click(within(dialog).getByRole("button", { name: "Remove alpha" }));
    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith("srv-a");
    });
  });

  it("prefills a stored sample override in edit mode and patches it when changed", async () => {
    const patchSpy = vi
      .spyOn(api, "patchServer")
      .mockResolvedValue(
        makeRecord({ server_id: "srv-a", display_name: "alpha", sample_interval_s: 10 }),
      );
    useConsoleStore.setState({
      servers: [
        makeRecord({ server_id: "srv-a", display_name: "alpha", sample_interval_s: 10 }),
      ],
      statuses: {},
    });
    render(<SettingsPage />);

    fireEvent.click(screen.getByLabelText("Server registry: alpha"));
    fireEvent.click(screen.getByText("Edit"));
    await screen.findByRole("dialog");

    expect((screen.getByLabelText("Sample interval (s)") as HTMLInputElement).value).toBe("10");

    fireEvent.change(screen.getByLabelText("Sample interval (s)"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(
        "srv-a",
        expect.objectContaining({ sample_interval_s: 5 }),
      );
    });
  });
});

describe("add server dialog: sample interval", () => {
  async function fillManualAndSubmit() {
    const createSpy = vi
      .spyOn(api, "createServer")
      .mockResolvedValue(makeRecord({ server_id: "srv-box", display_name: "Box" }));
    vi.spyOn(api, "testConnection").mockResolvedValue(OK_RESULT);
    render(<SettingsPage />);
    fireEvent.click(screen.getByRole("button", { name: "Add server" }));
    await screen.findByText("From ssh config");
    fireEvent.click(screen.getByRole("button", { name: "Manual" }));
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Box" } });
    fireEvent.change(screen.getByLabelText("Host"), { target: { value: "10.1.2.3" } });
    return createSpy;
  }

  it("includes sample_interval_s in the create payload when set", async () => {
    const createSpy = await fillManualAndSubmit();
    fireEvent.change(screen.getByLabelText("Sample interval (s)"), {
      target: { value: "5" },
    });
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalledWith(
        expect.objectContaining({ display_name: "Box", sample_interval_s: 5 }),
      );
    });
  });

  it("omits sample_interval_s from the payload when the field is empty", async () => {
    const createSpy = await fillManualAndSubmit();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Add & test connection" }),
    );

    await waitFor(() => {
      expect(createSpy).toHaveBeenCalled();
    });
    const body = createSpy.mock.calls[0][0];
    expect(Object.hasOwn(body, "sample_interval_s")).toBe(false);
  });

  it("shows an inline error and blocks submit for out-of-range values", async () => {
    await fillManualAndSubmit();
    const interval = screen.getByLabelText("Sample interval (s)");
    fireEvent.change(interval, { target: { value: "700" } });

    expect(screen.getByText("Interval must be 0.5–600 seconds")).toBeTruthy();
    const submit = within(screen.getByRole("dialog")).getByRole("button", {
      name: "Add & test connection",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    // back in range → the hint replaces the error and submit unlocks
    fireEvent.change(interval, { target: { value: "2.5" } });
    expect(screen.queryByText("Interval must be 0.5–600 seconds")).toBeNull();
    expect(screen.getByText(/Seconds between detail updates/)).toBeTruthy();
    expect(submit.disabled).toBe(false);
  });
});

describe("preferences: detail sections", () => {
  beforeEach(() => {
    act(() => {
      for (const tab of [...getHiddenSections()]) toggleSection(tab);
    });
  });

  afterEach(() => {
    act(() => {
      for (const tab of [...getHiddenSections()]) toggleSection(tab);
    });
  });

  it("renders four pressed chips and toggles hidden state with persistence", () => {
    render(<SettingsPage />);
    const group = screen.getByRole("group", { name: "Detail sections" });

    for (const label of ["Processes", "System", "Storage", "Network"]) {
      expect(within(group).getByRole("button", { name: label })).toBeTruthy();
    }
    expect(within(group).getByRole("button", { name: "Processes" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(within(group).getByRole("button", { name: "Processes" }));
    expect(within(group).getByRole("button", { name: "Processes" }).getAttribute("aria-pressed")).toBe("false");
    expect(getHiddenSections()).toEqual(["processes"]);
    expect(localStorage.getItem("sgc.sections")).toBe(JSON.stringify(["processes"]));

    // Overview and GPUs are never offered as toggleable chips
    expect(within(group).queryByRole("button", { name: "Overview" })).toBeNull();
    expect(within(group).queryByRole("button", { name: "GPUs" })).toBeNull();
  });
});

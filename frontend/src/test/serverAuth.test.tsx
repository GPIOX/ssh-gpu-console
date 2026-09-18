/**
 * Phase 4.2B/4.2D: ServerDialog 本机认证 (auth status rows + password
 * credential flow) and the shared DirectAuthPairRow. All server names are
 * synthetic fixtures. The password input must start empty, never be
 * prefilled from any API payload, leave no secret in the DOM after save,
 * and all direct-auth actions fire only on explicit clicks.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ServerDialog } from "../features/settings/ServerDialog";
import { DirectAuthPairRow, type DirectAuthAction } from "../features/settings/DirectAuthPairRow";
import { api } from "../services/api";
import { useConsoleStore } from "../store/consoleStore";
import type {
  AliasEntry,
  DirectAuthPair,
  DirectAuthPeer,
  ServerAuthStatus,
  ServerRecord,
} from "../types/models";

const ALIASES: AliasEntry[] = [
  { alias: "lab-node", host: "192.168.1.40", user: "demo", port: 22 },
];

function makeServer(overrides: Partial<ServerRecord> = {}): ServerRecord {
  return {
    server_id: "srv-auth",
    display_name: "Auth Node",
    ssh_host: "lab-node",
    username: "demo",
    port: 22,
    tags: [],
    enabled: true,
    ...overrides,
  };
}

function authFixture(overrides: Partial<ServerAuthStatus> = {}): ServerAuthStatus {
  return {
    ssh_config_used: true,
    effective_host: "192.168.1.40",
    effective_user: "demo",
    effective_port: 22,
    identity_files: 2,
    agent_available: true,
    proxy_jump_configured: true,
    password_configured: false,
    password_storage: null,
    ...overrides,
  };
}

function pairFixture(overrides: Partial<DirectAuthPair> = {}): DirectAuthPair {
  return {
    source_server_id: "src-1",
    target_server_id: "tgt-1",
    configured: false,
    method: null,
    available: null,
    reason: null,
    checked_at: null,
    ...overrides,
  };
}

function peerFixture(overrides: Partial<DirectAuthPeer> = {}): DirectAuthPeer {
  return {
    target_server_id: "tgt-1",
    configured: false,
    method: null,
    available: null,
    reason: null,
    checked_at: null,
    ...overrides,
  };
}

function pairRowProps(peer: DirectAuthPeer) {
  return {
    sourceId: "src-1",
    sourceName: "Source One",
    targetId: "tgt-1",
    peer,
  };
}

/** onAction built on real api spies, so click counts hit the service layer. */
function apiBackedOnAction() {
  return async (action: DirectAuthAction): Promise<DirectAuthPeer> => {
    if (action === "check") return api.checkDirectAuth("src-1", "tgt-1");
    if (action === "setup") return api.setupDirectKey("src-1", "tgt-1");
    await api.revokeDirectAuth("src-1", "tgt-1");
    return peerFixture();
  };
}

beforeEach(() => {
  vi.spyOn(api, "listSshAliases").mockResolvedValue(ALIASES);
  vi.spyOn(api, "getServerAuth").mockResolvedValue(authFixture());
  vi.spyOn(api, "getDirectAuth").mockResolvedValue({
    server_id: "srv-auth",
    pairs: [],
  });
  useConsoleStore.setState({ servers: [makeServer()] });
});

afterEach(() => {
  cleanup();
  useConsoleStore.setState({ servers: [] });
  vi.restoreAllMocks();
});

describe("ServerDialog 本机认证 status rows", () => {
  it("renders the resolved facts and unconfigured password state", async () => {
    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    await screen.findByText("matched");
    expect(screen.getByText("2 configured")).toBeTruthy();
    expect(screen.getByText("available")).toBeTruthy();
    expect(screen.getByText("configured", { selector: ".settings-auth__value" })).toBeTruthy();
    expect(screen.getByText("not set")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Set password" })).toBeTruthy();
    expect(api.getServerAuth).toHaveBeenCalledWith("srv-auth");
    // The auth endpoint never carries a password and the UI never asks for one.
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("renders the unmatched / un-detected / unavailable states", async () => {
    vi.spyOn(api, "getServerAuth").mockResolvedValue(
      authFixture({
        ssh_config_used: false,
        identity_files: 0,
        agent_available: false,
        proxy_jump_configured: false,
      }),
    );
    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    expect(await screen.findByText("not matched")).toBeTruthy();
    expect(screen.getByText("none detected")).toBeTruthy();
    expect(screen.getByText("unavailable")).toBeTruthy();
    expect(screen.getByText("none", { selector: ".settings-auth__value" })).toBeTruthy();
  });
});

describe("ServerDialog password credential flow", () => {
  it("opens an empty password input (never prefilled), saves via PUT, clears the input instantly", async () => {
    const setSpy = vi
      .spyOn(api, "setPassword")
      .mockResolvedValue({ configured: true, storage: "system_keyring" });
    // Refresh after save reports the configured state.
    vi.spyOn(api, "getServerAuth")
      .mockResolvedValueOnce(authFixture())
      .mockResolvedValue(authFixture({ password_configured: true, password_storage: "system_keyring" }));

    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Set password" }));

    const input = screen.getByLabelText("Password") as HTMLInputElement;
    expect(input.value).toBe(""); // starts empty — nothing to prefill from
    expect(input.type).toBe("password");
    expect(input.getAttribute("autocomplete")).toBe("new-password");

    fireEvent.change(input, { target: { value: "s3cret-pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(setSpy).toHaveBeenCalledWith("srv-auth", "s3cret-pw");
    });
    // After success the refreshed row shows bullets + storage word; the plain
    // secret is gone from the DOM.
    expect(await screen.findByText("••••••••")).toBeTruthy();
    expect(screen.getByText("saved securely")).toBeTruthy();
    expect(screen.getByText("system keyring")).toBeTruthy();
    expect(screen.queryByDisplayValue("s3cret-pw")).toBeNull();
    expect(screen.queryByLabelText("Password")).toBeNull();
  });

  it("shows session-only storage word and clears through an explicit confirm DELETE", async () => {
    const deleteSpy = vi.spyOn(api, "deletePassword").mockResolvedValue(undefined);
    vi.spyOn(api, "getServerAuth")
      .mockResolvedValueOnce(authFixture({ password_configured: true, password_storage: "session_only" }))
      .mockResolvedValue(authFixture());

    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    expect(await screen.findByText("this session only")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear password" }));

    // Two-step confirm: no DELETE until the explicit confirm click.
    expect(deleteSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm clear" }));

    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith("srv-auth");
    });
    expect(await screen.findByText("not set")).toBeTruthy();
  });

  it("starts with an empty input again after close + reopen (no lingering secret)", async () => {
    const { rerender } = render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Set password" }));
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "s3cret-pw" } });

    rerender(<ServerDialog open={false} onClose={() => {}} server={makeServer()} />);
    rerender(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    fireEvent.click(await screen.findByRole("button", { name: "Set password" }));
    const input = screen.getByLabelText("Password") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(screen.queryByDisplayValue("s3cret-pw")).toBeNull();
  });
});

describe("DirectAuthPairRow (explicit actions only)", () => {
  it("does not auto-check on mount and shows 未配置/not configured + one Check direct button", () => {
    const checkSpy = vi.spyOn(api, "checkDirectAuth");
    render(<DirectAuthPairRow {...pairRowProps(peerFixture())} onAction={apiBackedOnAction()} />);

    expect(screen.getByText("not configured")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check direct" })).toBeTruthy();
    expect(checkSpy).not.toHaveBeenCalled();
  });

  it("fires exactly one check on click and shows the native ready banner", async () => {
    const checkSpy = vi
      .spyOn(api, "checkDirectAuth")
      .mockResolvedValue(peerFixture({ method: "native", available: true, checked_at: "2026-09-18T00:00:00Z" }));

    render(<DirectAuthPairRow {...pairRowProps(peerFixture())} onAction={apiBackedOnAction()} />);
    fireEvent.click(screen.getByRole("button", { name: "Check direct" }));

    await waitFor(() => {
      expect(checkSpy).toHaveBeenCalledTimes(1);
      expect(checkSpy).toHaveBeenCalledWith("src-1", "tgt-1");
    });
    expect(
      await screen.findByText("✓ Ready for direct authentication — no setup needed"),
    ).toBeTruthy();
  });

  it("shows cannot-authenticate + the dedicated-key setup after an auth failure", async () => {
    const setupSpy = vi
      .spyOn(api, "setupDirectKey")
      .mockResolvedValue(
        peerFixture({ configured: true, method: "sgc_key", available: true, checked_at: "2026-09-18T00:00:00Z" }),
      );
    vi.spyOn(api, "checkDirectAuth").mockResolvedValue(
      peerFixture({ available: false, reason: "authentication_failed", checked_at: "2026-09-18T00:00:00Z" }),
    );

    render(<DirectAuthPairRow {...pairRowProps(peerFixture())} onAction={apiBackedOnAction()} />);
    fireEvent.click(screen.getByRole("button", { name: "Check direct" }));

    expect(await screen.findByText("Cannot authenticate directly")).toBeTruthy();
    expect(screen.getByText("authentication failed")).toBeTruthy(); // humanized reason
    fireEvent.click(screen.getByRole("button", { name: "Set up dedicated key" }));

    await waitFor(() => {
      expect(setupSpy).toHaveBeenCalledWith("src-1", "tgt-1");
    });
    expect(await screen.findByText("✓ Dedicated key in place")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Re-check" })).toBeTruthy();
  });

  it("configured sgc_key offers Re-check + Revoke, and revoke needs the confirm step", async () => {
    const revokeSpy = vi.spyOn(api, "revokeDirectAuth").mockResolvedValue(undefined);
    render(
      <DirectAuthPairRow
        {...pairRowProps(peerFixture({ configured: true, method: "sgc_key", available: true, checked_at: "2026-09-18T00:00:00Z" }))}
        onAction={apiBackedOnAction()}
      />,
    );

    expect(await screen.findByText("✓ Dedicated key in place")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));

    // Explicit confirm — no DELETE on the first click.
    expect(revokeSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));

    await waitFor(() => {
      expect(revokeSpy).toHaveBeenCalledTimes(1);
      expect(revokeSpy).toHaveBeenCalledWith("src-1", "tgt-1");
    });
    expect(await screen.findByText("not configured")).toBeTruthy();
  });

  it("humanizes known reason codes and falls back to the raw string", async () => {
    // Wire tolerance: an unrecognized code arrives as a raw string (the
    // normalizer passes it through; the type union lists only known codes).
    vi.spyOn(api, "checkDirectAuth").mockResolvedValue(
      peerFixture({ available: false, reason: "some_new_code" as DirectAuthPeer["reason"], checked_at: "2026-09-18T00:00:00Z" }),
    );
    render(<DirectAuthPairRow {...pairRowProps(peerFixture())} onAction={apiBackedOnAction()} />);

    fireEvent.click(screen.getByRole("button", { name: "Check direct" }));
    expect(await screen.findByText("some_new_code")).toBeTruthy();
  });

  it("humanizes the keygen_missing_source reason code", async () => {
    vi.spyOn(api, "checkDirectAuth").mockResolvedValue(
      peerFixture({
        available: false,
        reason: "keygen_missing_source",
        checked_at: "2026-09-18T00:00:00Z",
      }),
    );
    render(<DirectAuthPairRow {...pairRowProps(peerFixture())} onAction={apiBackedOnAction()} />);

    fireEvent.click(screen.getByRole("button", { name: "Check direct" }));
    expect(
      await screen.findByText("Source server lacks ssh-keygen; dedicated key cannot be generated"),
    ).toBeTruthy();
  });
});

describe("ServerDialog 直连传输 section (pairs shape)", () => {
  const PEER_SERVER = makeServer({ server_id: "srv-peer", display_name: "Peer Node" });

  it("seeds rows ONLY from incoming (target == this server) pairs", async () => {
    useConsoleStore.setState({ servers: [makeServer(), PEER_SERVER] });
    // Outgoing pair (self → peer) listed first and unchecked; only the
    // incoming pair (peer → self) may back the peer's row.
    vi.spyOn(api, "getDirectAuth").mockResolvedValue({
      server_id: "srv-auth",
      pairs: [
        pairFixture({ source_server_id: "srv-auth", target_server_id: "srv-peer" }),
        pairFixture({
          source_server_id: "srv-peer",
          target_server_id: "srv-auth",
          configured: true,
          method: "sgc_key",
          available: true,
          checked_at: "2026-09-18T00:00:00Z",
        }),
      ],
    });

    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    expect(api.getDirectAuth).toHaveBeenCalledWith("srv-auth");
    const row = await screen.findByText("Peer Node", { selector: ".da-pair__name" });
    const pairEl = row.closest(".da-pair");
    expect(pairEl?.getAttribute("data-source")).toBe("srv-peer");
    expect(pairEl?.getAttribute("data-target")).toBe("srv-auth");
    // Seeded from the INCOMING pair (dedicated key), not the outgoing pair.
    expect(await screen.findByText("✓ Dedicated key in place")).toBeTruthy();
    expect(document.querySelectorAll(".da-pair")).toHaveLength(1);

    // Explicit re-check targets the exact incoming pair endpoints.
    const checkSpy = vi
      .spyOn(api, "checkDirectAuth")
      .mockResolvedValue(peerFixture({ method: "native", available: true, checked_at: "2026-09-18T00:00:00Z" }));
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await waitFor(() => {
      expect(checkSpy).toHaveBeenCalledWith("srv-peer", "srv-auth");
    });
  });

  it("ignores outgoing (this server → other) pairs when seeding", async () => {
    useConsoleStore.setState({ servers: [makeServer(), PEER_SERVER] });
    vi.spyOn(api, "getDirectAuth").mockResolvedValue({
      server_id: "srv-auth",
      pairs: [
        pairFixture({
          source_server_id: "srv-auth",
          target_server_id: "srv-peer",
          configured: true,
          method: "sgc_key",
          available: true,
          checked_at: "2026-09-18T00:00:00Z",
        }),
      ],
    });

    render(<ServerDialog open onClose={() => {}} server={makeServer()} />);

    const row = await screen.findByText("Peer Node", { selector: ".da-pair__name" });
    expect(row.closest(".da-pair")?.getAttribute("data-source")).toBe("srv-peer");
    expect(await screen.findByText("not configured")).toBeTruthy();
    expect(screen.queryByText("✓ Dedicated key in place")).toBeNull();
  });
});

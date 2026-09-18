import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../services/api";
import type { ServerSnapshot } from "../types/models";

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<unknown>;

function jsonResponse(status: number, body: unknown): unknown {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 404 ? "Not Found" : "OK",
    json: async () => body,
  };
}

function stubFetch(impl: FetchLike): ReturnType<typeof vi.fn> {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api client", () => {
  it("lists servers with defaults filled in", async () => {
    stubFetch(async () =>
      jsonResponse(200, [{ server_id: "a", display_name: "Alpha", ssh_host: "alpha" }]),
    );
    const servers = await api.listServers();
    expect(servers).toEqual([
      {
        server_id: "a",
        display_name: "Alpha",
        ssh_host: "alpha",
        username: null,
        port: null,
        tags: [],
        enabled: true,
        sample_interval_s: null,
      },
    ]);
  });

  it("passes through only bounded numeric per-server sample intervals", async () => {
    stubFetch(async () =>
      jsonResponse(200, [
        { server_id: "a", display_name: "A", ssh_host: "a", sample_interval_s: 7.5 },
        { server_id: "b", display_name: "B", ssh_host: "b", sample_interval_s: 900 },
        { server_id: "c", display_name: "C", ssh_host: "c", sample_interval_s: "2.5" },
        { server_id: "d", display_name: "D", ssh_host: "d" },
      ]),
    );
    const servers = await api.listServers();
    expect(servers.map((server) => server.sample_interval_s)).toEqual([7.5, null, null, null]);
  });

  it("drops malformed registry entries instead of throwing", async () => {
    stubFetch(async () => jsonResponse(200, [{ nope: true }, "garbage", null]));
    const servers = await api.listServers();
    expect(servers).toEqual([]);
  });

  it("raises ApiError with status + detail", async () => {
    stubFetch(async () => jsonResponse(404, { detail: "nope" }));
    const error = await api.getSnapshot("ghost").catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(404);
    expect((error as ApiError).detail).toBe("nope");
  });

  it("maps network failure to ApiError status 0", async () => {
    stubFetch(async () => {
      throw new Error("boom");
    });
    const error = await api.getFleet().catch((cause: unknown) => cause);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).detail).toBe("boom");
  });

  it("rejects invalid snapshot payloads", async () => {
    stubFetch(async () => jsonResponse(200, { hello: "world" }));
    await expect(api.getSnapshot("a")).rejects.toThrow("invalid snapshot payload");
  });

  it("normalizes partial snapshots", async () => {
    const raw = { server_id: "a", status: "online", gpus: [{ index: 0 }] };
    stubFetch(async () => jsonResponse(200, raw));
    const snapshot = await api.getSnapshot("a");
    expect(snapshot.status).toBe("online");
    expect(snapshot.gpus).toHaveLength(1);
    expect(snapshot.gpus[0].availability).toBe("unavailable");
    expect(snapshot.gpus[0].utilization_percent).toBeNull();
    expect(snapshot.stale).toBe(false);
  });

  it("posts named action bodies", async () => {
    const fetchMock = stubFetch(async () => jsonResponse(204, undefined));
    await api.terminateProcess("srv1", 40211);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/servers/srv1/actions/terminate-process");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ pid: 40211 }));
  });

  it("encodes server ids in paths", async () => {
    const fetchMock = stubFetch(async () => jsonResponse(204, undefined));
    await api.deleteServer("srv/with space");
    expect((fetchMock.mock.calls[0] as unknown[])[0]).toBe("/api/v1/servers/srv%2Fwith%20space");
  });

  it("normalizes gpu history points", async () => {
    stubFetch(async () =>
      jsonResponse(200, [
        { t: "2026-01-01T00:00:00Z", v: 12.5 },
        { garbage: true },
        { t: "2026-01-01T00:00:05Z", v: "not-a-number" },
      ]),
    );
    const points = await api.getGpuHistory("srv1", 0, "utilization");
    expect(points).toEqual([{ t: "2026-01-01T00:00:00Z", v: 12.5 }]);
  });
});

describe("ApiError", () => {
  it("carries status and detail", () => {
    const error = new ApiError(500, "boom");
    expect(error.status).toBe(500);
    expect(error.detail).toBe("boom");
    expect(error.message).toBe("boom");
    expect(error.name).toBe("ApiError");
  });
});

describe("snapshot normalization invariants", () => {
  it("never leaves unknown fields untyped", async () => {
    const raw = {
      server_id: "a",
      generated_at: "2026-01-01T00:00:00Z",
      stale: "yes",
      errors: { gpu: "command_missing", bogus: 42 },
    };
    stubFetch(async () => jsonResponse(200, raw));
    const snapshot: ServerSnapshot = await api.getSnapshot("a");
    expect(snapshot.stale).toBe(false);
    expect(snapshot.errors).toEqual({ gpu: "command_missing" });
    expect(snapshot.cpu).toBeNull();
    expect(snapshot.gpus).toEqual([]);
  });
});

describe("direct-auth pairs listing normalization", () => {
  it("normalizes the pairs shape reading both ids, dropping malformed pairs", async () => {
    stubFetch(async () =>
      jsonResponse(200, {
        server_id: "srv-q",
        pairs: [
          {
            source_server_id: "srv-a",
            target_server_id: "srv-q",
            configured: true,
            method: "sgc_key",
            available: true,
            reason: null,
            checked_at: "2026-09-18T00:00:00Z",
          },
          {
            source_server_id: "srv-q",
            target_server_id: "srv-b",
            configured: false,
            method: "native",
            available: false,
            reason: "keygen_missing_source",
            checked_at: null,
          },
          { source_server_id: "srv-c" }, // missing target id → dropped
          "garbage", // dropped
        ],
      }),
    );
    const list = await api.getDirectAuth("srv-q");
    expect(list.server_id).toBe("srv-q");
    expect(list.pairs).toEqual([
      {
        source_server_id: "srv-a",
        target_server_id: "srv-q",
        configured: true,
        method: "sgc_key",
        available: true,
        reason: null,
        checked_at: "2026-09-18T00:00:00Z",
      },
      {
        source_server_id: "srv-q",
        target_server_id: "srv-b",
        configured: false,
        method: "native",
        available: false,
        reason: "keygen_missing_source",
        checked_at: null,
      },
    ]);
  });

  it("passes unknown reason codes through verbatim and rejects a missing server_id", async () => {
    stubFetch(async () =>
      jsonResponse(200, {
        server_id: "srv-q",
        pairs: [
          {
            source_server_id: "srv-a",
            target_server_id: "srv-q",
            configured: false,
            method: null,
            available: false,
            reason: "some_future_code",
            checked_at: null,
          },
        ],
      }),
    );
    const list = await api.getDirectAuth("srv-q");
    expect(list.pairs[0]?.reason).toBe("some_future_code");

    stubFetch(async () =>
      jsonResponse(200, { source_server_id: "srv-q", pairs: [] }), // legacy shape → invalid
    );
    await expect(api.getDirectAuth("srv-q")).rejects.toThrow("invalid direct-auth list payload");
  });
});

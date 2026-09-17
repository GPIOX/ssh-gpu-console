import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RealtimeClient, backoffDelay, type RealtimeSocket } from "../services/realtime";
import type { ServerToClientFrame } from "../types/models";

class FakeWebSocket implements RealtimeSocket {
  static instances: FakeWebSocket[] = [];

  static reset(): void {
    FakeWebSocket.instances = [];
  }

  readyState = 0;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  readonly sent: string[] = [];
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState !== 3) {
      this.readyState = 3;
      this.onclose?.();
    }
  }

  // test helpers
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  fail(): void {
    this.close();
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  receiveRaw(data: unknown): void {
    this.onmessage?.({ data });
  }
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances.at(-1);
  if (socket === undefined) throw new Error("no socket created");
  return socket;
}

function sentFrames(socket: FakeWebSocket): Array<Record<string, unknown>> {
  return socket.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>);
}

function makeClient(): RealtimeClient {
  return new RealtimeClient({
    url: "ws://test/api/v1/realtime",
    socketFactory: (url) => new FakeWebSocket(url),
  });
}

describe("backoffDelay", () => {
  it("doubles from 1s and caps at 30s", () => {
    expect(backoffDelay(0)).toBe(1_000);
    expect(backoffDelay(1)).toBe(2_000);
    expect(backoffDelay(2)).toBe(4_000);
    expect(backoffDelay(5)).toBe(30_000);
    expect(backoffDelay(50)).toBe(30_000);
  });
});

describe("RealtimeClient", () => {
  beforeEach(() => {
    FakeWebSocket.reset();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("connects and reports state transitions", () => {
    const client = makeClient();
    const states: string[] = [];
    client.onState((state) => states.push(state));
    client.start();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(states).toEqual(["connecting"]);
    lastSocket().open();
    expect(states).toEqual(["connecting", "live"]);
    client.stop();
  });

  it("sends the select intent when the socket opens", () => {
    const client = makeClient();
    client.sendSelect("srv1");
    client.start();
    expect(lastSocket().sent).toEqual([]);
    lastSocket().open();
    expect(sentFrames(lastSocket())).toEqual([{ type: "select", server_id: "srv1" }]);
    client.stop();
  });

  it("reconnects with exponential backoff and resets on open", () => {
    const client = makeClient();
    client.start();

    lastSocket().fail();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    lastSocket().fail();
    vi.advanceTimersByTime(1_999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    // successful open resets the backoff sequence
    lastSocket().open();
    lastSocket().fail();
    vi.advanceTimersByTime(1_000);
    expect(FakeWebSocket.instances).toHaveLength(4);
    client.stop();
  });

  it("replays the latest select intent after reconnect", () => {
    const client = makeClient();
    client.start();
    const first = lastSocket();
    first.open();
    client.sendSelect("srv2");
    expect(sentFrames(first)).toEqual([{ type: "select", server_id: "srv2" }]);

    first.fail();
    vi.advanceTimersByTime(1_000);
    const second = lastSocket();
    second.open();
    expect(sentFrames(second)).toEqual([{ type: "select", server_id: "srv2" }]);
    client.stop();
  });

  it("dispatches only valid frames and ignores malformed ones", () => {
    const client = makeClient();
    const frames: ServerToClientFrame[] = [];
    client.onFrame((frame) => frames.push(frame));
    client.start();
    const socket = lastSocket();
    socket.open();

    socket.receive({ type: "hello", server_ids: ["a", "b"] });
    socket.receiveRaw("not json");
    socket.receive({ type: "bogus" });
    socket.receive({ type: "server", server_id: "x" }); // missing snapshot
    socket.receive({ type: "server", server_id: "x", snapshot: { server_id: "x" } });
    socket.receive({ type: "status", server_id: "x", status: "online" });
    socket.receive(42);

    expect(frames).toEqual([
      { type: "hello", server_ids: ["a", "b"] },
      {
        type: "server",
        server_id: "x",
        snapshot: {
          server_id: "x",
          status: "unknown",
          generated_at: null,
          cpu: null,
          memory: null,
          gpus: [],
          gpu_processes: [],
          processes: [],
          storage: [],
          network: [],
          system: null,
          errors: {},
          stale: false,
        },
      },
      { type: "status", server_id: "x", status: "online" },
    ]);
    client.stop();
  });

  it("does not reconnect after stop", () => {
    const client = makeClient();
    client.start();
    client.stop();
    expect(lastSocket().readyState).toBe(3);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it("start is idempotent", () => {
    const client = makeClient();
    client.start();
    client.start();
    expect(FakeWebSocket.instances).toHaveLength(1);
    client.stop();
  });
});

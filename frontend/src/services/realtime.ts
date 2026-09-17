/**
 * The one shared WebSocket for the whole app. Reconnects with exponential
 * backoff (1s doubling, 30s cap, reset on successful open). Malformed frames
 * are ignored; the latest select intent is replayed after every reconnect.
 */

import type { ClientToServerFrame, ServerToClientFrame } from "../types/models";
import { normalizeFrame } from "./normalize";

export type RealtimeState = "connecting" | "live" | "reconnecting";

/** Minimal socket surface, so tests can inject a fake. */
export interface RealtimeSocket {
  readyState: number;
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
}

const OPEN = 1;
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
const MAX_ATTEMPT = 10; // 2**10 >> cap; keeps the exponent bounded

export function backoffDelay(attempt: number): number {
  return Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** attempt);
}

export function defaultRealtimeUrl(): string {
  const { protocol, host } = window.location;
  const scheme = protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${host}/api/v1/realtime`;
}

export interface RealtimeOptions {
  url?: string;
  socketFactory?: (url: string) => RealtimeSocket;
}

export class RealtimeClient {
  private readonly url: string;
  private readonly factory: (url: string) => RealtimeSocket;
  private socket: RealtimeSocket | null = null;
  private attempt = 0;
  private stopped = true;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly frameListeners = new Set<(frame: ServerToClientFrame) => void>();
  private readonly stateListeners = new Set<(state: RealtimeState) => void>();
  private intent: string | null = null;
  private hasIntent = false;

  constructor(options: RealtimeOptions = {}) {
    this.url = options.url ?? defaultRealtimeUrl();
    this.factory =
      options.socketFactory ??
      ((url) => new WebSocket(url) as unknown as RealtimeSocket);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.emitState("connecting");
    this.open();
  }

  stop(): void {
    this.stopped = true;
    this.clearTimer();
    const socket = this.socket;
    this.socket = null;
    socket?.close();
  }

  onFrame(listener: (frame: ServerToClientFrame) => void): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  onState(listener: (state: RealtimeState) => void): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  /** Records the select intent and sends it immediately when live. */
  sendSelect(serverId: string | null): void {
    this.intent = serverId;
    this.hasIntent = true;
    this.send({ type: "select", server_id: serverId });
  }

  sendPing(): void {
    this.send({ type: "ping" });
  }

  private send(frame: ClientToServerFrame): void {
    if (this.socket !== null && this.socket.readyState === OPEN) {
      this.socket.send(JSON.stringify(frame));
    }
  }

  private open(): void {
    if (this.stopped) return;
    let socket: RealtimeSocket;
    try {
      socket = this.factory(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket) return;
      this.attempt = 0; // backoff resets on open
      this.emitState("live");
      if (this.hasIntent) this.send({ type: "select", server_id: this.intent });
    };
    socket.onmessage = (event) => {
      const frame = this.parse(event.data);
      if (frame === null) return; // malformed frames are ignored
      for (const listener of this.frameListeners) listener(frame);
    };
    socket.onerror = () => {
      // close always follows an error; nothing to do here
    };
    socket.onclose = () => {
      if (this.socket !== socket) return; // stale socket
      this.socket = null;
      if (!this.stopped) {
        this.emitState("reconnecting");
        this.scheduleReconnect();
      }
    };
  }

  private parse(data: unknown): ServerToClientFrame | null {
    if (typeof data !== "string") return null;
    let raw: unknown;
    try {
      raw = JSON.parse(data);
    } catch {
      return null;
    }
    return normalizeFrame(raw);
  }

  private scheduleReconnect(): void {
    this.clearTimer();
    const delay = backoffDelay(this.attempt);
    this.attempt = Math.min(this.attempt + 1, MAX_ATTEMPT);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, delay);
  }

  private clearTimer(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private emitState(state: RealtimeState): void {
    for (const listener of this.stateListeners) listener(state);
  }
}

/** App-wide singleton. Tests construct their own RealtimeClient instances. */
export const realtime = new RealtimeClient();

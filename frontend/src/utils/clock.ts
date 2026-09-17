/**
 * One shared 1Hz clock for every relative "Xs ago" label — never per-row
 * timers (DESIGN.md performance law). Ticks are skipped while the document is
 * hidden; the store keeps updating from the WebSocket regardless.
 */

import { useSyncExternalStore } from "react";

const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let tick = 0;

function ensureTimer(): void {
  if (timer !== null) return;
  timer = setInterval(() => {
    if (document.visibilityState === "hidden") return; // skip visual commits
    tick += 1;
    for (const listener of listeners) listener();
  }, 1_000);
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  ensureTimer();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): number {
  return tick;
}

/** Re-renders the calling component once per second (when visible). */
export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot);
}

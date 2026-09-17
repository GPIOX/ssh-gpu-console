/**
 * Locale store + i18n hooks. Mirrors the theme controller's shape:
 * module-level store, localStorage persistence (UI preference, never
 * telemetry), `useSyncExternalStore` hooks. First visit follows
 * `navigator.language` (zh* → Chinese), then the user's choice sticks.
 */

import { en, type Dict } from "./en";
import { zh } from "./zh";

export type Locale = "en" | "zh";

const STORAGE_KEY = "sgc.locale";
const LOCALES: Locale[] = ["en", "zh"];

let current: Locale = load();
const listeners = new Set<() => void>();

function load(): Locale {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (LOCALES.includes(raw as Locale)) return raw as Locale;
  } catch {
    /* private mode */
  }
  const nav = typeof navigator !== "undefined" ? navigator.language : "en";
  return nav.toLowerCase().startsWith("zh") ? "zh" : "en";
}

function persist(): void {
  try {
    localStorage.setItem(STORAGE_KEY, current);
  } catch {
    /* private mode: session-only preference */
  }
}

export function getLocale(): Locale {
  return current;
}

export function getDict(): Dict {
  return current === "zh" ? zh : en;
}

export function setLocale(locale: Locale): void {
  if (!LOCALES.includes(locale) || locale === current) return;
  current = locale;
  persist();
  listeners.forEach((notify) => notify());
}

export function subscribeLocale(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

/** Interpolate `{placeholders}` in a dictionary template. */
export function tf(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in params ? String(params[key]) : match,
  );
}

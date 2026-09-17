/**
 * Theme controller: dark-first product with a user-selectable appearance
 * (dark | light | system). The preference persists in localStorage (UI
 * preference, not telemetry) and resolves to a `data-theme` attribute on
 * <html>; "system" tracks `prefers-color-scheme` live.
 */

export type ThemePreference = "dark" | "minimal" | "warm" | "system";

const STORAGE_KEY = "sgc.theme";
const THEME_ATTR = "data-theme";

const PREFERENCES: ThemePreference[] = ["dark", "minimal", "warm", "system"];

let current: ThemePreference = load();
const listeners = new Set<() => void>();
// jsdom (vitest) does not implement matchMedia; degrade to static dark.
const media: MediaQueryList | null =
  typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

function load(): ThemePreference {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    // Legacy "light" preferences resolve to the renamed Minimal theme.
    if (raw === "light") return "minimal";
    return PREFERENCES.includes(raw as ThemePreference)
      ? (raw as ThemePreference)
      : "dark";
  } catch {
    return "dark";
  }
}

function resolved(pref: ThemePreference): "dark" | "minimal" | "warm" {
  if (pref !== "system") return pref;
  return media?.matches ? "dark" : "minimal";
}

function apply(): void {
  document.documentElement.setAttribute(THEME_ATTR, resolved(current));
  // UI preference only — never telemetry.
  try {
    localStorage.setItem(STORAGE_KEY, current);
  } catch {
    /* private mode: session-only preference */
  }
  listeners.forEach((notify) => notify());
}

export function getThemePreference(): ThemePreference {
  return current;
}

export function getResolvedTheme(): "dark" | "minimal" | "warm" {
  return resolved(current);
}

export function setThemePreference(pref: ThemePreference): void {
  if (!PREFERENCES.includes(pref) || pref === current) return;
  current = pref;
  apply();
}

export function subscribeTheme(notify: () => void): () => void {
  listeners.add(notify);
  media?.addEventListener("change", apply);
  return () => {
    listeners.delete(notify);
    media?.removeEventListener("change", apply);
  };
}

/** Initial paint: run before React renders (called from main.tsx). */
export function initTheme(): void {
  apply();
}

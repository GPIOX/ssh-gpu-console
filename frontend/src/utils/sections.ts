/**
 * Detail-section visibility preference (Settings → Preferences). Overview and
 * GPUs are always visible; Processes/System/Storage/Network can be hidden.
 * The preference persists in localStorage (UI preference, never telemetry) as
 * a JSON array of hidden tab ids under `sgc.sections`; default = all visible.
 */

import { SERVER_TABS, type ServerTab } from "../shell/routes";

const STORAGE_KEY = "sgc.sections";

/** Tabs a user may hide; overview/gpus are excluded by design. */
export const TOGGLEABLE_SECTIONS: readonly ServerTab[] = [
  "processes",
  "system",
  "storage",
  "network",
];

const TAB_IDS: readonly string[] = SERVER_TABS;

function load(): ServerTab[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return [...new Set(
      parsed.filter(
        (item): item is ServerTab =>
          typeof item === "string" &&
          TOGGLEABLE_SECTIONS.includes(item as ServerTab) &&
          TAB_IDS.includes(item),
      ),
    )];
  } catch {
    return []; // corrupt payload behaves like "never configured"
  }
}

let current: ServerTab[] = load();
const listeners = new Set<() => void>();

function persist(): void {
  // UI preference only — never telemetry.
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* private mode: session-only preference */
  }
  listeners.forEach((notify) => notify());
}

export function getHiddenSections(): ServerTab[] {
  return current;
}

export function isSectionHidden(tab: ServerTab): boolean {
  return current.includes(tab);
}

export function toggleSection(tab: ServerTab): void {
  if (!TOGGLEABLE_SECTIONS.includes(tab)) return; // overview/gpus always visible
  current = current.includes(tab)
    ? current.filter((item) => item !== tab)
    : [...current, tab];
  persist();
}

export function subscribeSections(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

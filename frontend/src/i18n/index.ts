/**
 * i18n public surface. Components call `useT()` for the dictionary and
 * `useLocale()`/`setLocale()` for the switcher. Dictionary parity (zh must
 * match en key-for-key) is enforced by the `Dict` type at compile time.
 */

import { useSyncExternalStore } from "react";
import { getDict, getLocale, setLocale, subscribeLocale, type Locale } from "./locale";
import { tf } from "./locale";
import type { Dict } from "./en";

export type { Dict, Locale };

export function useT(): Dict {
  return useSyncExternalStore(subscribeLocale, getDict, getDict);
}

export function useLocale(): Locale {
  return useSyncExternalStore(subscribeLocale, getLocale, getLocale);
}

export { setLocale, tf };

/** Localized "Xs ago" ladder replacing formatRelative in components. */
export function useRelative(): (
  timestamp: string | number | null | undefined,
  now?: number,
) => string {
  const t = useT();
  return (timestamp, now = Date.now()) => {
    if (timestamp === null || timestamp === undefined) return "—";
    const time = typeof timestamp === "number" ? timestamp : Date.parse(timestamp);
    if (!Number.isFinite(time)) return "—";
    const seconds = Math.max(0, Math.round((now - time) / 1000));
    if (seconds < 5) return t.relative.now;
    if (seconds < 60) return tf(t.relative.s, { n: seconds });
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return tf(t.relative.m, { n: minutes });
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return tf(t.relative.h, { n: hours });
    return tf(t.relative.d, { n: Math.floor(hours / 24) });
  };
}

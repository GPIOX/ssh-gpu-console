/**
 * Shared workspace helpers: artifact labels, kind chips, and the field
 * validations that mirror the backend's wire-model constraints exactly
 * (no shell metacharacters in program/working_dir; roots 1–512 without
 * control characters). Validation here only pre-empts the server — the
 * server's verdict always wins.
 */

import type { Dict } from "../../i18n";
import type { ArtifactKind, ArtifactRecord } from "../../types/workspace";

/** `name` or `name:version` — never fabricates a version. */
export function artifactLabel(
  artifact: Pick<ArtifactRecord, "name" | "version">,
): string {
  return artifact.version !== null && artifact.version !== ""
    ? `${artifact.name}:${artifact.version}`
    : artifact.name;
}

export function kindLabel(t: Dict, kind: ArtifactKind): string {
  if (kind === "dataset") return t.workspace.kindDataset;
  if (kind === "model") return t.workspace.kindModel;
  return t.workspace.kindCode;
}

export function kindSectionTitle(t: Dict, kind: ArtifactKind): string {
  return kind === "model" ? t.workspace.tabModels : t.workspace.tabDatasets;
}

/** ServerRoots bound: 1–512 characters, no control characters. */
export function rootPathValid(raw: string): boolean {
  if (raw === "" || raw.length > 512) return false;
  for (const char of raw) {
    const code = char.charCodeAt(0);
    if (code <= 0x1f || code === 0x7f) return false;
  }
  return true;
}

/** Backend PlacementRecord path bound: 1–512 characters. */
export function remotePathValid(raw: string): boolean {
  return raw !== "" && raw.length <= 512;
}

/** The backend's shell-metacharacter blacklist for program / working_dir. */
const SHELL_META = new Set([
  ";",
  "&",
  "|",
  "<",
  ">",
  "`",
  "$",
  "\n",
  "\r",
  "\0",
  "(",
  ")",
  "{",
  "}",
  "[",
  "]",
  "*",
  "?",
  "~",
  '"',
  "'",
  "\\",
]);

export function hasShellMeta(value: string): boolean {
  for (const char of value) {
    if (SHELL_META.has(char)) return true;
  }
  return false;
}

/** Comma-split for tag / args textareas (same parse as the server dialog). */
export function splitList(raw: string): string[] {
  return raw
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item !== "");
}

/** GiB → bytes (exact integer math; min_vram_b is a byte bound). */
export function gibToBytes(raw: string): number | null {
  if (raw.trim() === "") return null;
  if (!/^\d+$/.test(raw.trim())) return null;
  return Number.parseInt(raw.trim(), 10) * 1024 ** 3;
}

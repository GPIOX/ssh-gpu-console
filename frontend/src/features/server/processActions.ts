/**
 * Named process actions (terminate = SIGTERM, kill = SIGKILL) and their
 * confirmation copy. DESIGN.md: destructive confirmations name ALL of action
 * verb + server + target (PID + process name); the confirm button carries the
 * verb ("Terminate process 40211") — never a generic "OK".
 *
 * All copy is built from the dictionary (see src/i18n), so every helper takes
 * the `Dict` from `useT()` as its first argument. Error mapping still matches
 * on the English API detail, but renders the localized message.
 */

import { ApiError } from "../../services/api";
import { tf, type Dict } from "../../i18n";

export type ProcessAction = "terminate" | "kill";

/** Past-tense verb for the success toast ("Terminated 40211 — done"). */
export function actionPastTense(t: Dict, action: ProcessAction): string {
  return action === "terminate" ? t.processes.pastTerminate : t.processes.pastKill;
}

/** Signal semantics shown in the confirmation dialog. */
export function actionSignalHint(t: Dict, action: ProcessAction): string {
  return action === "terminate" ? t.processes.terminateHint : t.processes.killHint;
}

/** Confirm-button label carrying the verb, e.g. "Terminate process 40211". */
export function confirmLabel(t: Dict, action: ProcessAction, pid: number): string {
  return tf(
    action === "terminate" ? t.processes.confirmTerminate : t.processes.confirmKill,
    { pid },
  );
}

/** Lead sentence naming action + PID + process name + server display name. */
export function dialogLead(
  t: Dict,
  action: ProcessAction,
  pid: number,
  processName: string | null,
  serverName: string,
): string {
  const name = processName ?? t.common.unknown;
  return tf(
    action === "terminate" ? t.processes.terminateLead : t.processes.killLead,
    { pid, name, server: serverName },
  );
}

/**
 * Human wording for a failed action, mapped from the API error detail.
 * Unrecognized details pass through verbatim (mono in the UI).
 */
export function describeActionError(t: Dict, cause: unknown): string {
  const detail =
    cause instanceof ApiError
      ? cause.detail
      : cause instanceof Error
        ? cause.message
        : t.processes.errRequestFailed;
  const lowered = detail.toLowerCase();
  if (lowered.includes("no such process")) {
    return t.processes.errNoSuchProcess;
  }
  if (lowered.includes("operation not permitted")) {
    return t.processes.errPermission;
  }
  if (lowered.includes("not found")) {
    return t.processes.errNotFound;
  }
  return detail;
}

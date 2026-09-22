/**
 * Direct-auth UI helpers shared by the pair row and the transfer dialog:
 * human words for the reason taxonomy, and the gate deciding when an
 * unavailable direct connection may offer the dedicated-key setup — only
 * reasons a setup can actually fix; route/rsync-class failures never invite
 * key setup.
 */

import type { Dict } from "../../i18n";

/** Human words for a taxonomy code; unknown codes render verbatim. */
export function reasonText(t: Dict, reason: string | null): string | null {
  if (reason === null || reason === "") return null;
  switch (reason) {
    case "route_unreachable":
      return t.serverAuth.reasonRouteUnreachable;
    case "host_key_unknown":
      return t.serverAuth.reasonHostKeyUnknown;
    case "host_key_mismatch":
      return t.serverAuth.reasonHostKeyMismatch;
    case "authentication_failed":
      return t.serverAuth.reasonAuthenticationFailed;
    case "rsync_missing_source":
      return t.serverAuth.reasonRsyncMissingSource;
    case "rsync_missing_target":
      return t.serverAuth.reasonRsyncMissingTarget;
    case "dedicated_key_missing":
      return t.serverAuth.reasonDedicatedKeyMissing;
    case "authorized_key_missing":
      return t.serverAuth.reasonAuthorizedKeyMissing;
    case "remote_key_invalid":
      return t.serverAuth.reasonRemoteKeyInvalid;
    case "source_known_hosts_missing":
      return t.serverAuth.reasonSourceKnownHostsMissing;
    case "keygen_missing_source":
      return t.serverAuth.reasonKeygenMissingSource;
    default:
      return reason;
  }
}

/**
 * Reasons a dedicated-key setup can actually fix. Route/host-mismatch/
 * rsync-missing/keygen-class failures are environmental: the setup button
 * must stay hidden for them.
 */
const SETUP_REASONS = new Set([
  "authentication_failed",
  "host_key_unknown",
  "dedicated_key_missing",
  "authorized_key_missing",
  "source_known_hosts_missing",
  "remote_key_invalid",
]);

export function setupReasonAllowed(reason: string | null): boolean {
  return reason !== null && SETUP_REASONS.has(reason);
}

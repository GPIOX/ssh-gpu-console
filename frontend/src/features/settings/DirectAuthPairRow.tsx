/**
 * One (source → target) direct-auth pair row, shared by the ServerDialog's
 * 直连传输 section and the transfer dialog's DirectAuthSetupDialog.
 * All statuses come from the pinned direct-auth APIs; every remote action
 * (check / setup / revoke) fires ONLY on an explicit click — nothing runs on
 * mount. Revoke requires an explicit two-step confirm. Humanized status words
 * come from the reason taxonomy; unknown codes fall back to the raw string.
 */

import { useState } from "react";
import { Button, Chip } from "../../design";
import { useT } from "../../i18n";
import type { DirectAuthPeer } from "../../types/models";

export type DirectAuthAction = "check" | "setup" | "revoke";

export interface DirectAuthPairRowProps {
  sourceId: string;
  sourceName: string;
  targetId: string;
  /** Latest known metadata; the row's local state after any explicit action. */
  peer: DirectAuthPeer;
  /** Runs one direct-auth API call and returns the fresh peer metadata. */
  onAction: (action: DirectAuthAction) => Promise<DirectAuthPeer>;
  /** Error text setter for the host surface (row renders it inline too). */
  onError?: (message: string | null) => void;
}

/** Human words for a taxonomy code; unknown codes render verbatim. */
export function reasonText(
  t: ReturnType<typeof useT>,
  reason: string | null,
): string | null {
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

export function DirectAuthPairRow({
  sourceId,
  sourceName,
  targetId,
  peer,
  onAction,
  onError,
}: DirectAuthPairRowProps) {
  const t = useT();
  const [current, setCurrent] = useState<DirectAuthPeer>(peer);
  const [busy, setBusy] = useState<DirectAuthAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmingRevoke, setConfirmingRevoke] = useState(false);
  // Keyed re-seed: a new source/target (or fresh upstream metadata) resets the
  // local view without re-running anything.
  const [seed, setSeed] = useState(`${sourceId}>${targetId}`);
  const nextSeed = `${sourceId}>${targetId}`;
  if (seed !== nextSeed) {
    setSeed(nextSeed);
    setCurrent(peer);
    setError(null);
    setBusy(null);
    setConfirmingRevoke(false);
  }

  const run = async (action: DirectAuthAction): Promise<void> => {
    setBusy(action);
    setError(null);
    onError?.(null);
    try {
      setCurrent(await onAction(action));
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "request failed";
      setError(message);
      onError?.(message);
    } finally {
      setBusy(null);
      setConfirmingRevoke(false);
    }
  };

  const reason = reasonText(t, current.reason);
  const checked = current.checked_at !== null || current.available !== null;

  // Status word + chip tone from the pinned metadata only.
  let statusWord: string;
  let tone: "ok" | "crit" | "neutral" = "neutral";
  if (current.configured && current.method === "sgc_key") {
    statusWord = t.serverAuth.directKeyOk;
    tone = "ok";
  } else if (current.configured && current.method === "native") {
    statusWord = t.serverAuth.directNativeOk;
    tone = "ok";
  } else if (checked && current.available === true && current.method === "native") {
    statusWord = t.serverAuth.directNativeOk;
    tone = "ok";
  } else if (checked && current.available === false) {
    statusWord = t.serverAuth.directUnavailable;
    tone = "crit";
  } else {
    statusWord = t.serverAuth.directNotConfigured;
  }

  // Outcome banner after an explicit check / setup.
  const nativeReady =
    !current.configured && current.method === "native" && current.available === true;
  const keyReady = current.configured && current.method === "sgc_key";
  const cannotAuth = !current.configured && checked && current.available === false;

  return (
    <div className="da-pair" data-source={sourceId} data-target={targetId}>
      <span className="da-pair__name">{sourceName}</span>
      <Chip tone={tone}>{statusWord}</Chip>
      {reason !== null && current.available === false && (
        <span className="da-pair__reason">{reason}</span>
      )}
      <span className="da-pair__spacer" />
      {busy === null && !confirmingRevoke && !current.configured && (
        <Button
          disabled={busy !== null}
          onClick={() => void run("check")}
        >
          {t.serverAuth.checkDirect}
        </Button>
      )}
      {busy === null && !confirmingRevoke && !current.configured && cannotAuth && (
        <Button
          disabled={busy !== null}
          onClick={() => void run("setup")}
        >
          {t.serverAuth.setupKey}
        </Button>
      )}
      {busy === null && !confirmingRevoke && current.configured && (
        <Button disabled={busy !== null} onClick={() => void run("check")}>
          {t.serverAuth.recheck}
        </Button>
      )}
      {busy === null && !confirmingRevoke && current.configured && (
        <Button
          disabled={busy !== null}
          onClick={() => setConfirmingRevoke(true)}
        >
          {t.serverAuth.revoke}
        </Button>
      )}
      {busy === null && confirmingRevoke && (
        <>
          <Button
            variant="primary"
            disabled={busy !== null}
            onClick={() => void run("revoke")}
          >
            {t.serverAuth.revokeConfirm}
          </Button>
          <Button disabled={busy !== null} onClick={() => setConfirmingRevoke(false)}>
            {t.common.cancel}
          </Button>
        </>
      )}
      {busy !== null && (
        <span className="da-pair__busy">
          {busy === "check"
            ? t.serverAuth.checking
            : busy === "setup"
              ? t.serverAuth.settingUp
              : t.serverAuth.revoking}
        </span>
      )}
      {nativeReady && (
        <p className="da-pair__ok">{t.serverAuth.directNativeReady}</p>
      )}
      {keyReady && <p className="da-pair__ok">{t.serverAuth.directKeyReady}</p>}
      {cannotAuth && !nativeReady && (
        <p className="da-pair__no">{t.serverAuth.directCannotAuth}</p>
      )}
      {error !== null && (
        <p className="field__error">
          {t.serverAuth.checkFailed}: {error}
        </p>
      )}
    </div>
  );
}

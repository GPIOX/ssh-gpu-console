/**
 * One (source → target) direct-auth pair row, shared by the ServerDialog's
 * 直连传输 section and the transfer dialog's DirectAuthSetupDialog.
 * All statuses come from the pinned direct-auth APIs; every remote action
 * (check / setup / revoke) fires ONLY on an explicit click — nothing runs on
 * mount. Revoke requires an explicit two-step confirm. Humanized status words
 * come from the reason taxonomy; unknown codes fall back to the raw string.
 */

import { useEffect, useRef, useState } from "react";
import { Button, Chip } from "../../design";
import { useT } from "../../i18n";
import type { DirectAuthPeer } from "../../types/models";
import { reasonText, setupReasonAllowed } from "./directAuthUi";

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
  // Parent-refresh sync (local optimistic state): with no action in flight
  // the incoming peer is the source of truth — fresh upstream metadata for
  // the SAME pair converges here (the assignment is idempotent), and a pair
  // switch also drops the two-step revoke confirm. While an action is in
  // flight the sync must skip: the action's own result lands via run() and
  // a parent refresh must not clobber it.
  const lastPair = useRef(`${sourceId}>${targetId}`);
  useEffect(() => {
    if (busy !== null) return;
    setCurrent(peer);
    setError(null);
    const pair = `${sourceId}>${targetId}`;
    if (lastPair.current !== pair) {
      lastPair.current = pair;
      setConfirmingRevoke(false);
    }
    // busy is deliberately read without being a dep: busy transitions must
    // not re-trigger a sync (that would revert a finished action to the
    // not-yet-refreshed prop), and the render firing this effect already
    // carries the current busy value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [peer, sourceId, targetId]);

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
  // A configured key whose latest explicit probe failed is dead — the chip
  // must say crit regardless of the configured method.
  if (checked && current.available === false) {
    tone = "crit";
  }

  // Outcome banner after an explicit check / setup: shows the LATEST explicit
  // check outcome; config-only readiness ("已就绪") only until the first check.
  const nativeReady =
    !current.configured && current.method === "native" && current.available === true;
  const cannotAuth = !current.configured && checked && current.available === false;
  // The setup CTA is only for failures a dedicated-key setup can fix;
  // route/rsync-class failures never invite key setup.
  const setupAllowed = cannotAuth && setupReasonAllowed(current.reason);

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
      {busy === null && !confirmingRevoke && setupAllowed && (
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
      {checked && current.available === false && current.configured && (
        <p className="da-pair__no">{t.serverAuth.directCheckFailed}</p>
      )}
      {checked && current.available === true && (current.configured || current.method === "native") && (
        <p className="da-pair__ok">{t.serverAuth.directVerified}</p>
      )}
      {current.configured && current.method === "sgc_key" && !checked && (
        <p className="da-pair__ready">{t.serverAuth.directKeyReady}</p>
      )}
      {nativeReady && (
        <p className="da-pair__ok">{t.serverAuth.directNativeReady}</p>
      )}
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

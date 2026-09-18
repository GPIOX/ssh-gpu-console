/**
 * Small dialog for ONE (source → target) direct-auth pair, opened from the
 * transfer dialog's plan area via 配置直连. Contains the same DirectAuthPairRow
 * used in the settings dialog: check → setup → revoke, explicit clicks only.
 * The row is seeded from the zero-SSH pairs listing (source id first, exact
 * (source → target) pair; falls back to the target id's listing). After a
 * successful change (check/setup/revoke) the caller is told via onReplan so it
 * can offer 重新规划 and re-run the plan request.
 */

import { useEffect, useState } from "react";
import { Button, Dialog } from "../../design";
import { useT } from "../../i18n";
import { api } from "../../services/api";
import type { DirectAuthPeer } from "../../types/models";
import { DirectAuthPairRow, type DirectAuthAction } from "../settings/DirectAuthPairRow";

const UNCHECKED = (targetId: string): DirectAuthPeer => ({
  target_server_id: targetId,
  configured: false,
  method: null,
  available: null,
  reason: null,
  checked_at: null,
});

export interface DirectAuthSetupDialogProps {
  open: boolean;
  onClose: () => void;
  sourceId: string;
  sourceName: string;
  targetId: string;
  targetName: string;
  /** Fired when the user asks to re-plan after a change (explicit click). */
  onReplan: () => void;
}

export function DirectAuthSetupDialog({
  open,
  onClose,
  sourceId,
  sourceName,
  targetId,
  targetName,
  onReplan,
}: DirectAuthSetupDialogProps) {
  const t = useT();
  const [initial, setInitial] = useState<DirectAuthPeer | null>(null);
  const [changed, setChanged] = useState(false);

  // Mount/open: seed the row from the zero-SSH pairs listing of the SOURCE id
  // (one GET, no SSH), picking the exact (source → target) pair. The listing
  // covers both directions, so the exact direction match is required; if that
  // pair is missing, fall back to the TARGET id's listing (the ServerDialog
  // direction) before degrading to an unchecked peer. Nothing auto-fires.
  useEffect(() => {
    if (!open) {
      setInitial(null);
      setChanged(false);
      return;
    }
    let cancelled = false;
    setInitial(null);
    setChanged(false);
    const seed = async (): Promise<DirectAuthPeer> => {
      const list = await api.getDirectAuth(sourceId);
      const found = list.pairs.find(
        (pair) => pair.source_server_id === sourceId && pair.target_server_id === targetId,
      );
      if (found !== undefined) return found;
      const fallback = await api.getDirectAuth(targetId);
      return (
        fallback.pairs.find(
          (pair) => pair.source_server_id === sourceId && pair.target_server_id === targetId,
        ) ?? UNCHECKED(targetId)
      );
    };
    seed()
      .then((peer) => {
        if (!cancelled) setInitial(peer);
      })
      .catch(() => {
        if (!cancelled) setInitial(UNCHECKED(targetId));
      });
    return () => {
      cancelled = true;
    };
  }, [open, sourceId, targetId]);

  const runAction = async (action: DirectAuthAction): Promise<DirectAuthPeer> => {
    const fresh =
      action === "check"
        ? await api.checkDirectAuth(sourceId, targetId)
        : action === "setup"
          ? await api.setupDirectKey(sourceId, targetId)
          : await api.revokeDirectAuth(sourceId, targetId).then(() => UNCHECKED(targetId));
    setChanged(true);
    return fresh;
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`${t.transfers.directSetupTitle}: ${sourceName} → ${targetName}`}
      width={420}
    >
      <div className="tf-da-setup">
        <p className="tf-da-setup__pair mono">
          {sourceName} → {targetName}
        </p>
        {initial === null ? (
          <p className="tf-da-setup__loading">{t.transfers.planning}</p>
        ) : (
          <DirectAuthPairRow
            sourceId={sourceId}
            sourceName={sourceName}
            targetId={targetId}
            peer={initial}
            onAction={runAction}
          />
        )}
        <div className="ws-dialog__actions">
          <Button onClick={onClose}>{t.common.cancel}</Button>
          {changed && (
            <Button
              variant="primary"
              onClick={() => {
                onClose();
                onReplan();
              }}
            >
              {t.transfers.replan}
            </Button>
          )}
        </div>
      </div>
    </Dialog>
  );
}

/**
 * Transfer Center page: aggregate header (进行中 / 等待 / 已完成 — mono
 * counts), one row per job, and the New transfer dialog. Loads the RAM-only
 * list on mount; the store arms its 1 Hz poller only while jobs are active
 * and it is cleared on unmount. Listing never triggers SSH.
 */

import { useEffect, useState } from "react";
import { Button, Dialog, EmptyState, ErrorPanel, Panel, Skeleton } from "../../design";
import { tf, useT } from "../../i18n";
import { useTransferStore } from "../../store/transferStore";
import { useWorkspaceStore } from "../../store/workspaceStore";
import { NewTransferDialog } from "./NewTransferDialog";
import { TransferJobRow } from "./TransferJobRow";
import "./transfers.css";

export function TransfersPage() {
  const t = useT();
  const jobs = useTransferStore((state) => state.jobs);
  const loading = useTransferStore((state) => state.loading);
  const error = useTransferStore((state) => state.error);
  const dialogOpen = useTransferStore((state) => state.dialog.open);
  const loadJobs = useTransferStore((state) => state.loadJobs);
  const stopPolling = useTransferStore((state) => state.stopPolling);
  const openNewTransfer = useTransferStore((state) => state.openNewTransfer);
  const cancelJob = useTransferStore((state) => state.cancelJob);
  const retryJob = useTransferStore((state) => state.retryJob);
  const clearHistory = useTransferStore((state) => state.clearHistory);
  const loadArtifacts = useWorkspaceStore((state) => state.loadArtifacts);
  const loadPlacements = useWorkspaceStore((state) => state.loadPlacements);

  // Named confirm dialog for the destructive clear-history action.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState<string | null>(null);

  const clear = async (): Promise<void> => {
    setClearing(true);
    setClearError(null);
    try {
      await clearHistory();
      setConfirmOpen(false);
    } catch (cause) {
      setClearError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setClearing(false);
    }
  };

  useEffect(() => {
    void loadJobs();
    // The New-Transfer dialog lists workspace assets; after a refresh a user
    // can land directly on this page, so prefetch the catalog (idempotent
    // GETs, never polled).
    void loadArtifacts();
    void loadPlacements();
    return () => stopPolling(); // page unmount clears the 1 Hz timer
  }, [loadJobs, loadArtifacts, loadPlacements, stopPolling]);

  // 进行中 = planning/running/verifying; 等待 = queued; 已完成 = completed.
  const counts = {
    active: jobs.filter(
      (job) => job.state === "planning" || job.state === "running" || job.state === "verifying",
    ).length,
    queued: jobs.filter((job) => job.state === "queued").length,
    completed: jobs.filter((job) => job.state === "completed").length,
  };
  // Clearable = terminal (completed/failed/cancelled) jobs in the RAM list.
  const terminalCount = jobs.filter(
    (job) => job.state === "completed" || job.state === "failed" || job.state === "cancelled",
  ).length;

  return (
    <div className="tf-page">
      <header className="tf-head">
        <span className="tf-head__counts mono tnum">
          {tf(t.transfers.countActive, { n: counts.active })} ·{" "}
          {tf(t.transfers.countQueued, { n: counts.queued })} ·{" "}
          {tf(t.transfers.countCompleted, { n: counts.completed })}
        </span>
        <div className="tf-head__spacer" />
        <Button onClick={() => setConfirmOpen(true)} disabled={terminalCount === 0}>
          {t.transfers.clearHistory}
        </Button>
        <Button variant="primary" onClick={() => openNewTransfer()}>
          {t.transfers.newTransfer}
        </Button>
      </header>

      {error !== null ? (
        <Panel>
          <ErrorPanel
            title={t.transfers.error}
            detail={error}
            onRetry={() => void loadJobs()}
            retryLabel={t.common.retry}
          />
        </Panel>
      ) : loading && jobs.length === 0 ? (
        <Panel>
          <div className="tf-skeleton" aria-label={t.transfers.loadingAria}>
            {[0, 1, 2].map((row) => (
              <div key={row} className="tf-skeleton__row">
                <Skeleton width={180} height={14} radius="5px" />
                <Skeleton width={280} height={12} />
                <Skeleton width={160} height={16} radius="8px" />
              </div>
            ))}
          </div>
        </Panel>
      ) : jobs.length === 0 ? (
        <Panel>
          <EmptyState
            title={t.transfers.empty}
            hint={t.transfers.emptyHint}
            action={
              <Button variant="primary" onClick={() => openNewTransfer()}>
                {t.transfers.newTransfer}
              </Button>
            }
          />
        </Panel>
      ) : (
        <div className="tf-list">
          {jobs.map((job) => (
            <TransferJobRow
              key={job.job_id}
              job={job}
              onCancel={(jobId) => void cancelJob(jobId)}
              onRetry={(jobId) => void retryJob(jobId)}
            />
          ))}
        </div>
      )}

      <NewTransferDialog open={dialogOpen} />

      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t.transfers.clearHistoryTitle}
      >
        <p className="ws-confirm__text">{t.transfers.clearHistoryBody}</p>
        {clearError !== null && <p className="field__error">{clearError}</p>}
        <div className="ws-dialog__actions">
          <Button onClick={() => setConfirmOpen(false)} disabled={clearing}>
            {t.common.cancel}
          </Button>
          <Button variant="primary" disabled={clearing} onClick={() => void clear()}>
            {t.transfers.clearHistoryConfirm}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

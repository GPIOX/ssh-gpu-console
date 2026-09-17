/**
 * One transfer job row (per DESIGN.md restraint): artifact label semibold,
 * the 源 ↓ 策略 ↓ 目标 flow, a continuous segmented meter over bytes (or
 * files when bytes are unknown), rate + ETA, a strategy chip, the mono
 * current path, verb-carrying Cancel while active, Retry on terminal states,
 * and the backend's error message verbatim on failed rows.
 */

import { Button, Chip, Panel, SegmentedMeter, type ChipTone } from "../../design";
import { tf, useT } from "../../i18n";
import { useConsoleStore } from "../../store/consoleStore";
import type { ServerRecord } from "../../types/models";
import type { TransferJob, TransferState, TransferStrategy } from "../../types/transfers";
import { isActiveTransferState } from "../../types/transfers";
import { formatBps, formatBytesPair } from "../../utils/format";

export function stateWord(t: ReturnType<typeof useT>, state: TransferState): string {
  switch (state) {
    case "queued":
      return t.transfers.stateQueued;
    case "planning":
      return t.transfers.statePlanning;
    case "running":
      return t.transfers.stateRunning;
    case "verifying":
      return t.transfers.stateVerifying;
    case "completed":
      return t.transfers.stateCompleted;
    case "failed":
      return t.transfers.stateFailed;
    case "cancelled":
      return t.transfers.stateCancelled;
  }
}

function stateTone(state: TransferState): ChipTone {
  switch (state) {
    case "completed":
      return "ok";
    case "failed":
      return "crit";
    case "cancelled":
      return "warn";
    case "running":
      return "accent";
    case "verifying":
    case "planning":
      return "cold";
    case "queued":
      return "neutral";
  }
}

export function strategyWord(t: ReturnType<typeof useT>, strategy: TransferStrategy): string {
  if (strategy === "direct_rsync") return t.transfers.strategyDirect;
  if (strategy === "local_relay") return t.transfers.strategyRelay;
  return t.transfers.strategyAuto;
}

/** Retry is accepted by the backend for failed/completed/cancelled jobs. */
export function isRetryableState(state: TransferState): boolean {
  return state === "failed" || state === "cancelled" || state === "completed";
}

export interface TransferJobRowProps {
  job: TransferJob;
  onCancel: (jobId: string) => void;
  onRetry: (jobId: string) => void;
}

export function TransferJobRow({ job, onCancel, onRetry }: TransferJobRowProps) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const serverName = (id: string): string =>
    servers.find((server: ServerRecord) => server.server_id === id)?.display_name ?? id;

  const used = job.strategy_used ?? job.strategy_requested;
  const strategy = strategyWord(t, used);

  // Continuous progress over the reported total; when bytes are unknown fall
  // back to file counts — never fabricate a percentage.
  const meter =
    job.bytes_total !== null && job.bytes_total > 0
      ? {
          value: job.bytes_done,
          max: job.bytes_total,
          valueText: formatBytesPair(job.bytes_done, job.bytes_total),
        }
      : job.files_total !== null && job.files_total > 0
        ? {
            value: job.files_done,
            max: job.files_total,
            valueText: tf(t.transfers.filesProgress, {
              done: job.files_done,
              total: job.files_total,
            }),
          }
        : { value: null, max: 100, valueText: formatBytesPair(job.bytes_done, null) };

  const active = isActiveTransferState(job.state);
  const etaMinutes =
    job.eta_s === null || !Number.isFinite(job.eta_s)
      ? null
      : Math.max(1, Math.ceil(job.eta_s / 60));

  return (
    <Panel className="tf-row">
      <div className="tf-row__main">
        <div className="tf-row__head">
          <span className="tf-row__name">{job.artifact_label}</span>
          <Chip tone={stateTone(job.state)}>{stateWord(t, job.state)}</Chip>
          <Chip mono>{strategy}</Chip>
        </div>
        <div className="tf-row__flow mono">
          <span>{serverName(job.source_server_id)}</span>
          <span className="tf-row__arrow" aria-hidden="true">
            ↓
          </span>
          <span>{strategy}</span>
          <span className="tf-row__arrow" aria-hidden="true">
            ↓
          </span>
          <span>{serverName(job.target_server_id)}</span>
        </div>
        <SegmentedMeter
          className="tf-meter"
          value={meter.value}
          max={meter.max}
          state="ok"
          valueText={meter.valueText}
          aria-label={job.artifact_label}
        />
        <div className="tf-row__meta mono tnum">
          {job.rate_bps !== null && <span>{formatBps(job.rate_bps)}</span>}
          {etaMinutes !== null && (
            <span>{tf(t.transfers.etaMinutes, { n: etaMinutes })}</span>
          )}
          {job.current_path !== null && job.current_path !== "" && (
            <span className="tf-row__path mono" title={job.current_path}>
              {job.current_path}
            </span>
          )}
        </div>
        {job.state === "failed" && job.error_message !== "" && (
          <p className="tf-row__error">{job.error_message}</p>
        )}
      </div>
      <div className="tf-row__actions">
        {active && (
          <Button onClick={() => onCancel(job.job_id)}>{t.transfers.cancelTransfer}</Button>
        )}
        {isRetryableState(job.state) && (
          <Button onClick={() => onRetry(job.job_id)}>{t.transfers.retryTransfer}</Button>
        )}
      </div>
    </Panel>
  );
}

/**
 * Transfer Center store (Phase 2): RAM-only job list plus dialog open/prefill
 * state. Polling law (docs/WORKSPACE_SYNC_PLAN.md §5): poll ~1 Hz ONLY while
 * any job is queued/planning/running/verifying; the timer is cleared when no
 * active jobs remain, on manual stop, and is never duplicated — a single
 * shared interval, not per-row timers.
 */

import { create } from "zustand";
import { transfersApi } from "../services/transfersApi";
import { TRANSFERS_HASH } from "../shell/routes";
import type { TransferJob, TransferPrefill, TransferRequest } from "../types/transfers";
import { isActiveTransferState } from "../types/transfers";

export const TRANSFER_POLL_MS = 1000;

export interface TransferDialogState {
  open: boolean;
  prefill: TransferPrefill;
}

export interface TransferStateShape {
  jobs: TransferJob[];
  loading: boolean;
  error: string | null;
  /** True only while the 1 Hz timer is armed. */
  polling: boolean;
  dialog: TransferDialogState;

  loadJobs: () => Promise<void>;
  /** Arm polling when active jobs exist, clear it otherwise. Idempotent. */
  syncPolling: () => void;
  stopPolling: () => void;

  cancelJob: (jobId: string) => Promise<void>;
  retryJob: (jobId: string) => Promise<void>;
  /** Clear terminal jobs from RAM history, then refresh the list. Returns the cleared count. */
  clearHistory: () => Promise<number>;
  /** Queue a job (202), then refresh the list — polling re-arms via loadJobs. */
  createTransfer: (body: TransferRequest) => Promise<string>;

  /** Menu entry from the workspace (同步到…): opens the dialog on #/transfers. */
  openNewTransfer: (prefill?: TransferPrefill) => void;
  closeNewTransfer: () => void;
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : "request failed";
}

export function createTransferStore() {
  let timer: ReturnType<typeof setInterval> | null = null;

  return create<TransferStateShape>()((set, get) => {
    const clearTimer = (): void => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const tick = async (): Promise<void> => {
      // Concurrency guard: a slow response must not stack refreshes.
      if (get().loading) return;
      await get().loadJobs();
    };

    return {
      jobs: [],
      loading: false,
      error: null,
      polling: false,
      dialog: { open: false, prefill: {} },

      loadJobs: async () => {
        set({ loading: true });
        try {
          const jobs = await transfersApi.listJobs();
          set({ jobs, error: null, loading: false });
          get().syncPolling();
        } catch (cause) {
          set({ error: errorMessage(cause), loading: false });
        }
      },

      syncPolling: () => {
        const active = get().jobs.some((job) => isActiveTransferState(job.state));
        if (active && timer === null) {
          timer = setInterval(() => void tick(), TRANSFER_POLL_MS);
          set({ polling: true });
        } else if (!active && timer !== null) {
          clearTimer();
          set({ polling: false });
        }
      },

      stopPolling: () => {
        clearTimer();
        set({ polling: false });
      },

      cancelJob: async (jobId) => {
        await transfersApi.cancel(jobId);
        await get().loadJobs();
      },

      retryJob: async (jobId) => {
        await transfersApi.retry(jobId);
        await get().loadJobs();
      },

      clearHistory: async () => {
        const cleared = await transfersApi.clearHistory();
        await get().loadJobs();
        return cleared;
      },

      createTransfer: async (body) => {
        const jobId = await transfersApi.create(body);
        await get().loadJobs();
        return jobId;
      },

      openNewTransfer: (prefill = {}) => {
        set({ dialog: { open: true, prefill } });
        // The dialog lives on the Transfer Center page; navigate there unless
        // already present (same-hash navigation fires no hashchange event).
        if (window.location.hash !== TRANSFERS_HASH) {
          window.location.hash = TRANSFERS_HASH;
        }
      },

      closeNewTransfer: () => {
        set({ dialog: { open: false, prefill: {} } });
      },
    };
  });
}

export type TransferStoreApi = ReturnType<typeof createTransferStore>;

/** App-wide singleton. Tests use createTransferStore() for a fresh instance. */
export const useTransferStore = createTransferStore();

/** Module-level 同步到… entry for workspace rows (no hook wiring needed). */
export function openNewTransfer(prefill: TransferPrefill = {}): void {
  useTransferStore.getState().openNewTransfer(prefill);
}

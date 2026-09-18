/**
 * Add/Edit server dialog — the onboarding core (DESIGN.md "Onboarding & actions").
 * Primary path: pick an alias from ~/.ssh/config (GET /servers/ssh-config/aliases
 * with `alias — user@host:port` annotations); secondary path: manual host/user/port.
 * Add mode: submit → create → the dialog auto-runs the connection test inline.
 * Edit mode: "Save changes" patches and closes; "Test connection" tests the stored
 * record. Failure results carry the explicit error taxonomy help, and a pending
 * host key opens the TOFU security panel — trust only ever happens on an explicit
 * click, never automatically. All user-visible copy is dictionary-driven (useT/tf);
 * raw API error strings stay as machine diagnostics.
 *
 * Phase 4.2B/4.2D: below the basic fields, two compact sections —
 * 本机认证 (zero-SSH auth facts + the password credential flow) and
 * 直连传输 (one row per pair whose TARGET is this server, from the zero-SSH
 * pairs listing; every remote action is an explicit click, revoke is
 * confirmed). Password state lives in React state only — never in storage;
 * the input starts and stays empty until the user types.
 */

import { useEffect, useState } from "react";
import {
  Button,
  Chip,
  Dialog,
  ErrorPanel,
  Field,
  Select,
  Skeleton,
  Spinner,
  TextInput,
} from "../../design";
import { tf, useT } from "../../i18n";
import { api } from "../../services/api";
import { useConsoleStore } from "../../store/consoleStore";
import type {
  AliasEntry,
  ConnectionTestResult,
  DirectAuthPair,
  DirectAuthPeer,
  ServerAuthStatus,
  ServerCreate,
  ServerPatch,
  ServerRecord,
  ServerStatus,
} from "../../types/models";
import { cx } from "../../utils/cx";
import { DirectAuthPairRow, type DirectAuthAction } from "./DirectAuthPairRow";
import "./settings.css";

type SshSource = "alias" | "manual";
type Step = "form" | "testing" | "result";

/** Global default for the per-server fast-cadence override (backend contract). */
const DEFAULT_SAMPLE_INTERVAL_S = 2.5;

interface FormState {
  name: string;
  source: SshSource;
  /** Selected alias (alias mode); also seeds ssh_host on submit. */
  alias: string;
  /** Literal hostname (manual mode). */
  host: string;
  username: string;
  port: string;
  tags: string;
  /** Raw seconds text; empty = use the global default. */
  sampleInterval: string;
  enabled: boolean;
}

const EMPTY_FORM: FormState = {
  name: "",
  source: "alias",
  alias: "",
  host: "",
  username: "",
  port: "",
  tags: "",
  sampleInterval: "",
  enabled: true,
};

function formFromServer(server: ServerRecord): FormState {
  return {
    name: server.display_name,
    source: "manual",
    alias: server.ssh_host,
    host: server.ssh_host,
    username: server.username ?? "",
    port: server.port !== null ? String(server.port) : "",
    tags: server.tags.join(", "),
    sampleInterval:
      server.sample_interval_s !== null && server.sample_interval_s !== undefined
        ? String(server.sample_interval_s)
        : "",
    enabled: server.enabled,
  };
}

/** `user@host:port` annotation, omitting absent parts (never fabricates). */
function aliasEndpoint(entry: AliasEntry): string {
  const user = entry.user !== null ? `${entry.user}@` : "";
  const host = entry.host ?? entry.alias;
  const port = entry.port !== null ? `:${entry.port}` : "";
  return `${user}${host}${port}`;
}

function parseTags(raw: string): string[] {
  return raw
    .split(",")
    .map((tag) => tag.trim())
    .filter((tag) => tag !== "");
}

function parsePort(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d+$/.test(trimmed)) return "invalid";
  const value = Number.parseInt(trimmed, 10);
  return value >= 1 && value <= 65535 ? value : "invalid";
}

/** Sample interval in seconds: empty → null (default), else a finite number
 *  within 0.5–600 or "invalid" (non-numeric / out of range). */
function parseSampleInterval(raw: string): number | null | "invalid" {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (!/^\d*\.?\d+$/.test(trimmed)) return "invalid";
  const value = Number(trimmed);
  return value >= 0.5 && value <= 600 ? value : "invalid";
}

/** Help text per failure status — auth problems read differently from unreachable. */
function helpFor(t: ReturnType<typeof useT>, status: ServerStatus): string {
  switch (status) {
    case "authentication_failed":
      return t.dialog.authHelp;
    case "timeout":
    case "offline":
      return t.dialog.unreachableHelp;
    default:
      return t.dialog.genericHelp;
  }
}

export interface ServerDialogProps {
  open: boolean;
  onClose: () => void;
  /** Server record in edit mode; null in add mode. */
  server: ServerRecord | null;
}

// ---------------------------------------------------------------------------
// 本机认证 — zero-SSH facts + the password credential flow (explicit only)
// ---------------------------------------------------------------------------

type PasswordUIState =
  | { phase: "idle"; editing: false }
  | { phase: "idle"; editing: true }
  | { phase: "saving" }
  | { phase: "clearing" };

/** One small fact row: label left, quiet value right. */
function AuthRow({ label, value, tone }: { label: string; value: string; tone?: "ok" | "muted" }) {
  return (
    <div className="settings-auth__row">
      <span className="settings-auth__label">{label}</span>
      <span className={cx("settings-auth__value", tone === "ok" && "settings-auth__value--ok")}>
        {value}
      </span>
    </div>
  );
}

function LocalAuthSection({
  serverId,
  auth,
  authError,
  onReloadAuth,
}: {
  serverId: string;
  auth: ServerAuthStatus | null;
  authError: string | null;
  onReloadAuth: () => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [password, setPassword] = useState("");
  const [ui, setUi] = useState<PasswordUIState>({ phase: "idle", editing: false });
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingClear, setConfirmingClear] = useState(false);

  // Unmount (or target change) drops every password trace instantly.
  useEffect(() => {
    return () => {
      setPassword("");
      setEditing(false);
      setUi({ phase: "idle", editing: false });
      setActionError(null);
      setConfirmingClear(false);
    };
  }, [serverId]);

  const configured = auth?.password_configured === true;
  const busy = ui.phase === "saving" || ui.phase === "clearing";

  const save = async (): Promise<void> => {
    if (password === "") return;
    setUi({ phase: "saving" });
    setActionError(null);
    try {
      await api.setPassword(serverId, password);
      setPassword(""); // the secret never lingers in the DOM or state
      setEditing(false);
      setUi({ phase: "idle", editing: false });
      onReloadAuth();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "request failed");
      setUi({ phase: "idle", editing: true });
    }
  };

  const clear = async (): Promise<void> => {
    setUi({ phase: "clearing" });
    setActionError(null);
    try {
      await api.deletePassword(serverId);
      setConfirmingClear(false);
      setUi({ phase: "idle", editing: false });
      onReloadAuth();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : "request failed");
      setUi({ phase: "idle", editing: false });
    }
  };

  return (
    <div className="settings-auth" data-testid="local-auth-section">
      <p className="settings-auth__title micro-label">{t.serverAuth.localAuth}</p>
      {authError !== null ? (
        <ErrorPanel
          title={t.serverAuth.passwordLoadFailed}
          detail={authError}
          onRetry={onReloadAuth}
          retryLabel={t.common.retry}
        />
      ) : auth === null ? (
        <Skeleton height={72} radius="7px" />
      ) : (
        <>
          <AuthRow
            label={t.serverAuth.sshConfig}
            value={auth.ssh_config_used ? t.serverAuth.sshConfigMatched : t.serverAuth.sshConfigNotMatched}
            tone={auth.ssh_config_used ? "ok" : undefined}
          />
          <AuthRow
            label={t.serverAuth.identity}
            value={
              auth.identity_files > 0
                ? tf(t.serverAuth.identityConfigured, { n: auth.identity_files })
                : t.serverAuth.identityNone
            }
            tone={auth.identity_files > 0 ? "ok" : undefined}
          />
          <AuthRow
            label={t.serverAuth.agent}
            value={auth.agent_available ? t.serverAuth.agentAvailable : t.serverAuth.agentUnavailable}
            tone={auth.agent_available ? "ok" : undefined}
          />
          <AuthRow
            label={t.serverAuth.proxyJump}
            value={auth.proxy_jump_configured ? t.serverAuth.proxyJumpOn : t.serverAuth.proxyJumpOff}
          />
          <div className="settings-auth__row settings-auth__row--password" data-testid="password-row">
            <span className="settings-auth__label">{t.serverAuth.password}</span>
            {!configured && !editing && (
              <>
                <span className="settings-auth__value">{t.serverAuth.passwordUnset}</span>
                <span className="da-pair__spacer" />
                <Button
                  disabled={busy}
                  onClick={() => {
                    setPassword("");
                    setEditing(true);
                  }}
                >
                  {t.serverAuth.setPassword}
                </Button>
              </>
            )}
            {configured && !editing && (
              <>
                <span className="settings-auth__value settings-auth__bullets mono">••••••••</span>
                <span className="settings-auth__value settings-auth__value--ok">
                  {t.serverAuth.passwordSet}
                </span>
                <span className="settings-auth__value">
                  {auth.password_storage === "session_only"
                    ? t.serverAuth.storageSession
                    : auth.password_storage === "system_keyring"
                      ? t.serverAuth.storageKeyring
                      : ""}
                </span>
                <span className="da-pair__spacer" />
                <Button
                  disabled={busy}
                  onClick={() => {
                    setPassword("");
                    setEditing(true);
                  }}
                >
                  {t.serverAuth.replacePassword}
                </Button>
                {confirmingClear ? (
                  <>
                    <Button variant="primary" disabled={busy} onClick={() => void clear()}>
                      {t.serverAuth.clearPasswordConfirm}
                    </Button>
                    <Button disabled={busy} onClick={() => setConfirmingClear(false)}>
                      {t.common.cancel}
                    </Button>
                  </>
                ) : (
                  <Button disabled={busy} onClick={() => setConfirmingClear(true)}>
                    {t.serverAuth.clearPassword}
                  </Button>
                )}
              </>
            )}
            {editing && (
              <span className="settings-auth__editor">
                <TextInput
                  type="password"
                  autoComplete="new-password"
                  placeholder={t.serverAuth.passwordPlaceholder}
                  aria-label={t.serverAuth.password}
                  value={password}
                  disabled={busy}
                  onChange={(event) => setPassword(event.target.value)}
                />
                <Button
                  variant="primary"
                  disabled={busy || password === ""}
                  onClick={() => void save()}
                >
                  {ui.phase === "saving" ? t.serverAuth.passwordSaving : t.serverAuth.passwordSave}
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => {
                    setPassword("");
                    setEditing(false);
                  }}
                >
                  {t.common.cancel}
                </Button>
              </span>
            )}
          </div>
          {actionError !== null && (
            <p className="field__error">
              {configured ? t.serverAuth.passwordClearFailed : t.serverAuth.passwordSaveFailed}:{" "}
              {actionError}
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 直连传输 — other (enabled) servers → this server; explicit checks only.
// The zero-SSH pairs listing covers BOTH directions involving this server, so
// only pairs whose TARGET is this server seed the rows: a pair (thisServer →
// other) belongs to the other server's own dialog, never to this section.
// ---------------------------------------------------------------------------

function DirectTransferSection({ targetId }: { targetId: string }) {
  const t = useT();
  const servers = useConsoleStore((state) => state.servers);
  const [pairs, setPairs] = useState<DirectAuthPair[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const sources = servers.filter((s) => s.enabled && s.server_id !== targetId);

  useEffect(() => {
    let cancelled = false;
    setPairs(null);
    setListError(null);
    api
      .getDirectAuth(targetId)
      .then((list) => {
        if (!cancelled) setPairs(list.pairs);
      })
      .catch((cause) => {
        if (!cancelled) {
          setListError(cause instanceof Error ? cause.message : "request failed");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [targetId]);

  const runAction = async (
    sourceId: string,
    action: DirectAuthAction,
  ): Promise<DirectAuthPeer> => {
    const fresh =
      action === "check"
        ? await api.checkDirectAuth(sourceId, targetId)
        : action === "setup"
          ? await api.setupDirectKey(sourceId, targetId)
          : await (async () => {
              await api.revokeDirectAuth(sourceId, targetId);
              // After revoke the pair is back to "not configured, never checked".
              return {
                target_server_id: targetId,
                configured: false,
                method: null,
                available: null,
                reason: null,
                checked_at: null,
              } satisfies DirectAuthPeer;
            })();
    if (action !== "revoke") {
      // Refresh the section's cached pair metadata with the fresh peer.
      setPairs((current) =>
        (current ?? []).map((pair) =>
          pair.source_server_id === sourceId && pair.target_server_id === targetId
            ? { ...pair, ...fresh }
            : pair,
        ),
      );
    } else {
      setPairs((current) =>
        (current ?? []).map((pair) =>
          pair.source_server_id === sourceId && pair.target_server_id === targetId
            ? {
                ...pair,
                configured: false,
                method: null,
                available: null,
                reason: null,
                checked_at: null,
              }
            : pair,
        ),
      );
    }
    return fresh;
  };

  // Only incoming pairs (this server is the TARGET) back rows in this section.
  const incomingPairs = (pairs ?? []).filter((pair) => pair.target_server_id === targetId);

  return (
    <div className="settings-direct" data-testid="direct-section">
      <p className="settings-auth__title micro-label">{t.serverAuth.directTransfer}</p>
      <p className="settings-direct__intro">{t.serverAuth.directIntro}</p>
      {listError !== null ? (
        <ErrorPanel
          title={t.serverAuth.directListFailed}
          detail={listError}
          onRetry={() => {
            setListError(null);
            api
              .getDirectAuth(targetId)
              .then((list) => setPairs(list.pairs))
              .catch((cause) =>
                setListError(cause instanceof Error ? cause.message : "request failed"),
              );
          }}
          retryLabel={t.common.retry}
        />
      ) : pairs === null ? (
        <Skeleton height={40} radius="7px" />
      ) : sources.length === 0 ? (
        <p className="settings-direct__empty">{t.serverAuth.noSources}</p>
      ) : (
        sources.map((source) => {
          const pair =
            incomingPairs.find((candidate) => candidate.source_server_id === source.server_id) ?? {
              source_server_id: source.server_id,
              target_server_id: targetId,
              configured: false,
              method: null,
              available: null,
              reason: null,
              checked_at: null,
            };
          return (
            <DirectAuthPairRow
              key={source.server_id}
              sourceId={source.server_id}
              sourceName={source.display_name}
              targetId={targetId}
              peer={pair}
              onAction={(action) => runAction(source.server_id, action)}
            />
          );
        })
      )}
    </div>
  );
}

export function ServerDialog({ open, onClose, server }: ServerDialogProps) {
  const t = useT();
  const editing = server !== null;

  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [step, setStep] = useState<Step>("form");
  const [aliases, setAliases] = useState<AliasEntry[] | null>(null);
  const [aliasesError, setAliasesError] = useState<string | null>(null);
  /** Server id the inline test runs against (created record, or the edited one). */
  const [activeId, setActiveId] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [trustError, setTrustError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [trusting, setTrusting] = useState(false);
  // Phase 4.2B: zero-SSH auth facts, fetched when the dialog opens for a server.
  const [auth, setAuth] = useState<ServerAuthStatus | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);

  const loadAuth = (serverId: string) => {
    setAuth(null);
    setAuthError(null);
    api
      .getServerAuth(serverId)
      .then((status) => setAuth(status))
      .catch((cause) =>
        setAuthError(cause instanceof Error ? cause.message : "request failed"),
      );
  };

  // Re-seed the whole dialog on every open; aliases are re-read fresh.
  // Closing drops every password trace: state is reset before the early return.
  useEffect(() => {
    if (!open) {
      setAuth(null);
      setAuthError(null);
      return;
    }
    setForm(server === null ? EMPTY_FORM : formFromServer(server));
    setActiveId(server?.server_id ?? null);
    setStep("form");
    setResult(null);
    setTestError(null);
    setTrustError(null);
    setSubmitError(null);
    setSaving(false);
    setTrusting(false);
    setAliases(null);
    setAliasesError(null);
    if (server !== null) loadAuth(server.server_id);
    let cancelled = false;
    api
      .listSshAliases()
      .then((list) => {
        if (cancelled) return;
        setAliases(list);
        // Edit mode: snap onto the matching alias when the stored host is one.
        if (
          server !== null &&
          list.some((entry) => entry.alias === server.ssh_host)
        ) {
          setForm((current) =>
            current.source === "manual"
              ? { ...current, source: "alias", alias: server.ssh_host }
              : current,
          );
        }
      })
      .catch((cause) => {
        if (cancelled) return;
        setAliasesError(cause instanceof Error ? cause.message : "request failed");
      });
    return () => {
      cancelled = true;
    };
  }, [open, server]);

  const applyAlias = (alias: string) => {
    setForm((current) => {
      const entry = aliases?.find((candidate) => candidate.alias === alias);
      if (entry === undefined) return { ...current, alias };
      // The alias fills ssh_host (and the host override) and pre-fills
      // username/port when the entry provides them.
      return {
        ...current,
        alias,
        host: entry.alias,
        username: entry.user ?? "",
        port: entry.port !== null ? String(entry.port) : "",
      };
    });
  };

  const setSource = (source: SshSource) => {
    setForm((current) => ({ ...current, source }));
  };

  const loadAliases = () => {
    setAliases(null);
    setAliasesError(null);
    api
      .listSshAliases()
      .then((list) => setAliases(list))
      .catch((cause) =>
        setAliasesError(cause instanceof Error ? cause.message : "request failed"),
      );
  };

  const refreshRegistry = () => {
    const store = useConsoleStore.getState();
    void store.loadServers();
    void store.loadFleet();
  };

  const buildBody = (): ServerCreate => {
    const port = parsePort(form.port);
    const interval = parseSampleInterval(form.sampleInterval);
    // Override semantics: set → number; cleared in edit mode → explicit null
    // (the backend PATCH uses exclude_unset, so an explicit null resets the
    // stored override to the global default); add mode → key omitted.
    return {
      display_name: form.name.trim(),
      ssh_host: form.source === "alias" ? form.alias.trim() : form.host.trim(),
      username: form.username.trim() === "" ? null : form.username.trim(),
      port: port === "invalid" ? null : port,
      tags: parseTags(form.tags),
      enabled: form.enabled,
      ...(typeof interval === "number"
        ? { sample_interval_s: interval }
        : server !== null
          ? { sample_interval_s: null }
          : {}),
    };
  };

  const runTest = async (serverId: string): Promise<void> => {
    setStep("testing");
    setResult(null);
    setTestError(null);
    setTrustError(null);
    try {
      setResult(await api.testConnection(serverId));
    } catch (cause) {
      setTestError(cause instanceof Error ? cause.message : "request failed");
    } finally {
      setStep("result");
    }
  };

  const submit = async (): Promise<void> => {
    setSubmitError(null);
    setSaving(true);
    try {
      if (server === null) {
        const record = await api.createServer(buildBody());
        setActiveId(record.server_id);
        refreshRegistry();
        setSaving(false);
        await runTest(record.server_id); // add mode: test the fresh record inline
      } else {
        const body = buildBody();
        const patch: ServerPatch = {
          display_name: body.display_name,
          ssh_host: body.ssh_host,
          username: body.username,
          port: body.port,
          tags: body.tags,
          enabled: body.enabled,
          ...(body.sample_interval_s !== undefined
            ? { sample_interval_s: body.sample_interval_s }
            : {}),
        };
        await api.patchServer(server.server_id, patch);
        refreshRegistry();
        setSaving(false);
        onClose();
      }
    } catch (cause) {
      setSubmitError(cause instanceof Error ? cause.message : "request failed");
      setSaving(false);
    }
  };

  const trustAndRetry = async (): Promise<void> => {
    const prompt = result?.pending_host_key;
    if (activeId === null || prompt === null || prompt === undefined) return;
    setTrusting(true);
    setTrustError(null);
    try {
      await api.trustHostKey(activeId, prompt);
      await runTest(activeId);
    } catch (cause) {
      setTrustError(cause instanceof Error ? cause.message : "trust request failed");
    } finally {
      setTrusting(false);
    }
  };

  const portValid = parsePort(form.port) !== "invalid";
  const intervalValid = parseSampleInterval(form.sampleInterval) !== "invalid";
  const hostFilled =
    form.source === "alias" ? form.alias.trim() !== "" : form.host.trim() !== "";
  const canSubmit =
    form.name.trim() !== "" && hostFilled && portValid && intervalValid && !saving && !trusting;

  const title = editing
    ? tf(t.dialog.editTitle, { name: server.display_name })
    : t.dialog.addTitle;
  const selectedAlias =
    form.alias === "" ? undefined : aliases?.find((entry) => entry.alias === form.alias);

  return (
    <Dialog open={open} onClose={onClose} title={title} width={520}>
      {step === "form" && (
        <form
          className="settings-dialog__form"
          onSubmit={(event) => {
            event.preventDefault();
            if (canSubmit) void submit();
          }}
        >
          <Field label={t.dialog.displayName} htmlFor="srv-name">
            <TextInput
              id="srv-name"
              value={form.name}
              placeholder={t.dialog.namePlaceholder}
              onChange={(event) =>
                setForm((current) => ({ ...current, name: event.target.value }))
              }
            />
          </Field>

          <Field label={t.dialog.source}>
            <div className="settings-seg" role="group" aria-label={t.dialog.source}>
              <button
                type="button"
                className={cx(
                  "settings-seg__btn",
                  form.source === "alias" && "settings-seg__btn--active",
                )}
                aria-pressed={form.source === "alias"}
                onClick={() => setSource("alias")}
              >
                {t.dialog.fromConfig}
              </button>
              <button
                type="button"
                className={cx(
                  "settings-seg__btn",
                  form.source === "manual" && "settings-seg__btn--active",
                )}
                aria-pressed={form.source === "manual"}
                onClick={() => setSource("manual")}
              >
                {t.dialog.manual}
              </button>
            </div>
          </Field>

          {form.source === "alias" ? (
            aliasesError !== null ? (
              <ErrorPanel
                title={t.dialog.aliasesError}
                detail={aliasesError}
                onRetry={loadAliases}
                retryLabel={t.common.retry}
              />
            ) : aliases === null ? (
              <Skeleton height={32} radius="7px" />
            ) : aliases.length === 0 ? (
              <div className="settings-dialog__noalias">
                <p>{t.dialog.aliasEmpty}</p>
                <Button onClick={() => setSource("manual")}>{t.dialog.switchToManual}</Button>
              </div>
            ) : (
              <Field
                label={t.dialog.aliasLabel}
                htmlFor="srv-alias"
                hint={
                  selectedAlias !== undefined
                    ? aliasEndpoint(selectedAlias)
                    : t.dialog.aliasHint
                }
              >
                <Select
                  id="srv-alias"
                  value={form.alias}
                  onChange={(event) => applyAlias(event.target.value)}
                >
                  <option value="">{t.dialog.aliasPlaceholder}</option>
                  {aliases.map((entry) => (
                    <option key={entry.alias} value={entry.alias}>
                      {entry.alias} — {aliasEndpoint(entry)}
                    </option>
                  ))}
                </Select>
              </Field>
            )
          ) : (
            <Field label={t.dialog.host} htmlFor="srv-host">
              <TextInput
                id="srv-host"
                value={form.host}
                placeholder={t.dialog.hostPlaceholder}
                onChange={(event) =>
                  setForm((current) => ({ ...current, host: event.target.value }))
                }
              />
            </Field>
          )}

          <div className="settings-dialog__pair">
            <Field
              label={form.source === "alias" ? t.dialog.username : t.dialog.usernameOptional}
              htmlFor="srv-user"
              hint={form.source === "alias" ? t.dialog.aliasPrefill : undefined}
            >
              <TextInput
                id="srv-user"
                value={form.username}
                autoComplete="off"
                spellCheck={false}
                onChange={(event) =>
                  setForm((current) => ({ ...current, username: event.target.value }))
                }
              />
            </Field>
            <Field
              label={form.source === "alias" ? t.dialog.port : t.dialog.portOptional}
              htmlFor="srv-port"
              hint={form.source === "alias" ? t.dialog.aliasPrefill : undefined}
              error={parsePort(form.port) === "invalid" ? t.dialog.portInvalid : null}
            >
              <TextInput
                id="srv-port"
                type="number"
                min={1}
                max={65535}
                placeholder={t.dialog.portPlaceholder}
                value={form.port}
                onChange={(event) =>
                  setForm((current) => ({ ...current, port: event.target.value }))
                }
              />
            </Field>
          </div>

          <div className="settings-dialog__pair">
            <Field label={t.dialog.tags} htmlFor="srv-tags" hint={t.dialog.tagCommaHint}>
              <TextInput
                id="srv-tags"
                value={form.tags}
                placeholder={t.dialog.tagsPlaceholder}
                onChange={(event) =>
                  setForm((current) => ({ ...current, tags: event.target.value }))
                }
              />
            </Field>
            <Field
              label={t.dialog.sampleInterval}
              htmlFor="srv-interval"
              hint={t.dialog.sampleIntervalHint}
              error={parseSampleInterval(form.sampleInterval) === "invalid"
                ? t.dialog.sampleIntervalInvalid
                : null}
            >
              <TextInput
                id="srv-interval"
                type="number"
                min={0.5}
                max={600}
                step={0.5}
                placeholder={String(DEFAULT_SAMPLE_INTERVAL_S)}
                value={form.sampleInterval}
                onChange={(event) =>
                  setForm((current) => ({ ...current, sampleInterval: event.target.value }))
                }
              />
            </Field>
          </div>

          <Field label={t.dialog.enabled} hint={t.dialog.enabledHint}>
            <button
              type="button"
              role="switch"
              aria-checked={form.enabled}
              aria-label={t.dialog.enabled}
              className={cx("settings-switch", form.enabled && "settings-switch--on")}
              onClick={() =>
                setForm((current) => ({ ...current, enabled: !current.enabled }))
              }
            >
              <span className="settings-switch__thumb" />
            </button>
          </Field>

          {editing && server !== null && (
            <>
              <div className="settings-dialog__rule" role="presentation" />
              <LocalAuthSection
                serverId={server.server_id}
                auth={auth}
                authError={authError}
                onReloadAuth={() => loadAuth(server.server_id)}
              />
              <div className="settings-dialog__rule" role="presentation" />
              <DirectTransferSection targetId={server.server_id} />
            </>
          )}

          {submitError !== null && <p className="field__error">{submitError}</p>}

          <div className="settings-dialog__actions">
            {editing && (
              <Button
                disabled={saving || activeId === null}
                onClick={() => activeId !== null && void runTest(activeId)}
              >
                {t.dialog.testConnection}
              </Button>
            )}
            <div className="settings-dialog__spacer" />
            <Button onClick={onClose} disabled={saving}>
              {t.common.cancel}
            </Button>
            <Button variant="primary" type="submit" disabled={!canSubmit}>
              {saving ? t.dialog.saving : editing ? t.dialog.save : t.dialog.createAndTest}
            </Button>
          </div>
        </form>
      )}

      {step === "testing" && (
        <div className="settings-test__pending">
          <Spinner size={14} label={t.dialog.testing} />
          <span>{t.dialog.testing}</span>
        </div>
      )}

      {step === "result" && (
        <div className="settings-test">
          {testError !== null ? (
            <ErrorPanel
              title={t.dialog.testFailed}
              detail={testError}
              onRetry={
                activeId !== null ? () => void runTest(activeId) : undefined
              }
              retryLabel={t.dialog.testAgain}
            />
          ) : result !== null && result.pending_host_key !== null ? (
            <div className="settings-hostkey" role="alert">
              <p className="settings-hostkey__title">
                {tf(t.dialog.hostKeyTitle, { host: result.pending_host_key.host })}
              </p>
              <div className="settings-hostkey__facts">
                <span>
                  {tf(t.dialog.hostKeyBody, { type: result.pending_host_key.key_type })}
                </span>
                {result.pending_host_key.port !== null && (
                  <>
                    <span className="micro-label">{t.dialog.port}</span>
                    <span className="mono tnum">{result.pending_host_key.port}</span>
                  </>
                )}
              </div>
              <code className="settings-hostkey__fp">{result.pending_host_key.fingerprint}</code>
              <p className="settings-hostkey__advice">{t.dialog.hostKeyHint}</p>
              {trustError !== null && <p className="field__error">{trustError}</p>}
              <div className="settings-hostkey__actions">
                <Button
                  variant="primary"
                  disabled={trusting}
                  onClick={() => void trustAndRetry()}
                >
                  {trusting ? t.dialog.trusting : t.dialog.hostKeyTrust}
                </Button>
                <Button disabled={trusting} onClick={() => setStep("form")}>
                  {t.common.cancel}
                </Button>
              </div>
            </div>
          ) : result !== null && result.ok ? (
            <div className="settings-test__block">
              <div className="settings-test__line">
                <Chip tone="ok" mono>
                  {t.dialog.connected}
                </Chip>
                {result.latency_ms !== null && (
                  <span className="settings-test__latency mono tnum">
                    {tf(t.dialog.latency, { ms: Math.round(result.latency_ms) })}
                  </span>
                )}
              </div>
              {result.detail !== "" && (
                <p className="settings-test__detail">{result.detail}</p>
              )}
            </div>
          ) : result !== null ? (
            <div className="settings-test__block">
              <div className="settings-test__line">
                <Chip tone="crit" mono>
                  {t.status[result.status]}
                </Chip>
                {result.latency_ms !== null && (
                  <span className="settings-test__latency mono tnum">
                    {tf(t.dialog.latency, { ms: Math.round(result.latency_ms) })}
                  </span>
                )}
              </div>
              {result.detail !== "" && (
                <p className="settings-test__detail">{result.detail}</p>
              )}
              <p className="settings-test__help">{helpFor(t, result.status)}</p>
            </div>
          ) : null}

          <div className="settings-dialog__actions">
            {testError === null &&
              result !== null &&
              !result.ok &&
              result.pending_host_key === null && (
                <>
                  <Button onClick={() => setStep("form")}>{t.dialog.backToEdit}</Button>
                  <Button
                    disabled={activeId === null}
                    onClick={() => activeId !== null && void runTest(activeId)}
                  >
                    {t.dialog.testAgain}
                  </Button>
                </>
              )}
            <div className="settings-dialog__spacer" />
            <Button variant="primary" onClick={onClose}>
              {t.common.done}
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

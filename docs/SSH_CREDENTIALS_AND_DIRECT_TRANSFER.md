# SSH Credentials & Direct Transfer (Phase 4.2)

This document describes how the console authenticates to servers (LOCAL auth),
how a source server authenticates to a target server for direct rsync (DIRECT
auth), and why the two are separate concepts with separate lifecycles.

The design goal is **not** "make direct rsync work at any cost". It is: direct
rsync works only under secure, explicitly authorized, revocable conditions.
When direct authentication is unavailable or unconfigured, **Local Relay is
always the legitimate, safe fallback** — nothing degrades below it.

## 1. Two authentication domains

```
A. LOCAL AUTH                       B. DIRECT AUTH
GPU Console → Server                Source Server → Target Server
telemetry, SFTP, local relay,       DIRECT_RSYNC only
server management
```

A stored password (optional) belongs to LOCAL auth only. A dedicated transfer
key belongs to DIRECT auth only. UI, API, models and this document never mix
the two: a local password is never used for server-to-server rsync, and a
direct key is never used for console→server management.

## 2. Local authentication (console → server)

Precedence, unchanged from earlier phases: SSH config (`~/.ssh/config`),
IdentityFile, SSH agent — with an **optional** password as additional
authentication material, tried by AsyncSSH alongside the existing methods.
Key/agent auth remains primary; adding a password never removes key auth.

### CredentialStore

- `app/credentials/` exposes a small protocol: `has/get/set/delete_password`
  plus `storage_mode()`.
- Secure backend detection (`detect_secure_backend`) allows ONLY genuine OS
  keyrings: macOS Keychain, Linux Secret Service, Windows Credential Manager.
  Fail/null/chainer/file-based plaintext backends are rejected. There is **no
  plaintext-file fallback** — without a secure backend the store is
  `SessionMemoryCredentialStore` (RAM only; a backend restart loses it, and
  the UI says "仅本次会话" instead of claiming permanent storage).
- Keyring namespace is fixed: service `ssh-gpu-console`, account
  `server:<server_id>:password` — never the hostname alone, so two servers
  with the same host string cannot collide.
- Passwords are `SecretStr` on the wire (`PUT`/`DELETE`
  `/api/v1/servers/{id}/credentials/password`), unwrapped exactly once at the
  asyncssh call site inside the connect factory. The password never appears
  in `registry.json`, `workspace.json`, logs, exception messages, `repr()`,
  or any API response; setting or deleting a password closes that server's
  connection so the next use re-authenticates with the new material.
- Deleting a server deletes its keychain entry best-effort; server deletion
  itself always succeeds.

`GET /api/v1/servers/{id}/auth` reports aggregated local-auth facts
(ssh_config match, identity count, agent availability, ProxyJump, password
configured + storage mode) — counts and booleans only, never key content.

## 3. Direct authentication methods

`DirectAuthMethod`: `native` | `sgc_key`; absent → unavailable.

1. **native** — the source can already reach the target
   non-interactively (`ssh -o BatchMode=yes -o StrictHostKeyChecking=yes`).
   Nothing is configured or changed; this is always tried first.
2. **sgc_key** — the user explicitly clicks 配置专用传输密钥. The console
   provisions one ED25519 keypair **on the source server**:
   - key directory: `~/.ssh/gpu-console/keys/` (0700), key file mode 0600;
   - comment/comment-tag: `sgc-direct:<source-id>:<target-id>:<key-id>`;
   - private key is unencrypted by design (automation-only, non-interactive
     rsync) — therefore dedicated per pair, restricted at authorization,
     explicit setup, easy revoke, and never the user's personal key;
   - only the PUBLIC key is read back to the console (for fingerprint +
     authorized_keys installation). The private key never travels to the
     console and is never copied to any machine other than the source.
   - The public key is appended to the target's `~/.ssh/authorized_keys`
     with restrictions: `no-agent-forwarding,no-port-forwarding,
     no-X11-forwarding,no-pty` — one line, unique comment, idempotent,
     preserving every existing line byte-for-byte.
3. **unavailable** — anything else; transfers fall back to Local Relay.

## 3. Host-key trust for the direct path

Direct rsync keeps `StrictHostKeyChecking=yes`. Instead of disabling
verification or blindly key-scanning, the console exports the **already
verified** target host key from its own trusted local connection
(`SSHClientConnection.get_server_host_key()`) and installs it into the
source's app-owned `~/.ssh/gpu-console/known_hosts` (0600, `[host]:port`
notation for non-22 ports). The user's own `~/.ssh/known_hosts` on the
source is never touched.

## 4. Direct rsync integration

The planner probes native auth first, then the dedicated key (only when the
pair is configured), and records the winning method:

- `TransferPlan.direct_auth_method`: `native` | `sgc_key` | null
- rsync `-e` options for the dedicated path: `-i <key> -o IdentitiesOnly=yes
  -o UserKnownHostsFile=<sgc-known-hosts>` — plus the standing
  `-o BatchMode=yes -o StrictHostKeyChecking=yes`.
- When a run loses its configured key between planning and execution the job
  fails with an explicit error instead of silently degrading.

The transfer UI shows a humanized reason when direct is unavailable and
keeps the raw diagnostic (e.g. `Permission denied (publickey,password)`) as
secondary detail, with a 配置直连 entry point to the setup dialog.

## 5. Setup, repair, revoke

- **Setup** is a transaction: console→source and console→target reachability,
  verified host key capture, key generation (skipped when the pair key
  exists), public-key read, restricted authorized_keys append (temp file 0600
  before atomic rename), known_hosts install, final dedicated check, and only
  then persistence of non-secret metadata in `backend/data/direct_auth.json`
  (paths, key id, fingerprint, timestamps — never the private key).
- **Idempotency**: a configured pair whose dedicated check passes returns
  `already_configured` — no second key is ever created. A broken pair is
  repaired by revoking only the pair's own materials and re-running setup;
  transient route failures never destroy a working key.
- **Revoke** removes exactly one line from the target's `authorized_keys`
  (matched by the pair's exact comment AND fingerprint) and the pair's own
  key files on the source. User keys, other pairs' keys, and user
  `known_hosts` are never touched. Partial failures are reported; a revoke
  that could not prove full removal never claims success.
- **Server deletion** removes local credential and metadata bookkeeping only;
  it never performs remote revocation across machines. The UI hints to
  revoke direct relationships before deleting a server.

## 6. Threat model summary

- The local password is a secret with OS-keychain or session-only storage;
  no plaintext fallback exists anywhere.
- The dedicated key is an automation key with a narrow grant (fixed
  restrictions, single pair), never the user's personal identity, always
  user-initiated, fingerprint-auditable, and revocable from the UI.
- Host-key verification is never disabled anywhere in the codebase; an
  automated repo audit asserts this (no `StrictHostKeyChecking=no`,
  `UserKnownHostsFile=/dev/null`, `sshpass`, `SSH_ASKPASS`, `ForwardAgent=yes`
  anywhere in the source tree).
- The console never needs to see any private key beyond the user's own local
  SSH configuration, and never sees the dedicated private key at all.

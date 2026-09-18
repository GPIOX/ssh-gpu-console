"""Transfer planning: DIRECT_RSYNC preflight + AUTO fallback (Sol contract).

Direct rsync is selected only when EVERY condition holds: rsync on both ends,
strict non-interactive source→target SSH (BatchMode + StrictHostKeyChecking=yes,
never writing to known_hosts), no credential copying. Any preflight failure
falls back to Local Relay without failing the transfer.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import TYPE_CHECKING

from app.collectors.base import ExecutorLike
from app.models.transfer import DedicatedKeyOptions, TransferJob, TransferPlan, TransferStrategy
from app.ssh.file_transfer import ServerLike

if TYPE_CHECKING:
    from app.ssh.manager import SshManager
    from app.ssh.transport import LongCommandSession

_RSYNC_CHECK = "command -v rsync"
_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_DEFAULT_PROBE_TIMEOUT_S = 15.0
_PROBE_DETAIL_MAX_CHARS = 160


def build_rsync_command(
    *,
    source_path: str,
    target_spec: str,
    target_port: int | None,
    excludes: list[str] | tuple[str, ...] = (),
    dedicated_key: DedicatedKeyOptions | None = None,
    source_is_dir: bool = False,
) -> str:
    """Fixed-flag rsync command with safely quoted arguments (no user flags).

    Flags are deliberately explicit: `-l` copies symlinks AS links (never
    follows them — matches the relay's "never escape the transfer root"
    policy) and `--safe-links` makes the receiver refuse absolute or
    parent-escaping links; `-t` keeps mtimes for later incremental syncs.
    Owner/group/device preservation (-a/-o/-g) is dropped: unprivileged
    users would fail. Resume via --partial/--partial-dir; never --delete;
    never --append/--append-verify. The target port travels ONLY via the
    `-e` ssh options; the remote spec never embeds a port (rsync would
    treat everything after the first colon as path).
    Exclusion patterns travel as --exclude=arg (full rsync semantics; the
    transfer root itself is never excluded by rsync).

    ``source_is_dir`` appends the trailing slash to the SOURCE spec: without
    it, `rsync -r src dst` nests the copy as ``dst/<basename(src)>/``
    (field-verified even when dst does not exist), disagreeing with the
    relay's "contents land at target_path" semantics. A slash-source copies
    the CONTENTS into dst — the shared contract across both strategies.

    With ``dedicated_key`` the `-e` ssh options additionally pin the SGC
    dedicated identity and the app-owned known_hosts file (Phase 4.2C):
    `-i <key> -o IdentitiesOnly=yes -o UserKnownHostsFile=<known_hosts>`.
    NEVER any password-based auth, no askpass helpers, never agent forwarding.
    """
    source_spec = (
        source_path
        if source_path.endswith("/")
        else (f"{source_path}/" if source_is_dir else source_path)
    )
    ssh_opts = "ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes"
    if dedicated_key is not None:
        ssh_opts += (
            f" -i {shlex.quote(dedicated_key.private_key_path)}"
            " -o IdentitiesOnly=yes"
            f" -o UserKnownHostsFile={shlex.quote(dedicated_key.known_hosts_path)}"
        )
    if target_port:
        ssh_opts += f" -p {int(target_port)}"
    parts = [
        "rsync",
        "-r",
        "-l",
        "-t",
        "-p",
        "--safe-links",
        "--partial",
        "--partial-dir=.sgc-rsync-partial",
        "--info=progress2",
        "-e",
        shlex.quote(ssh_opts),
        *(_exclude_args(excludes)),
        "--",
        shlex.quote(source_spec),
        target_spec,  # already quoted by target_rsync_spec
    ]
    return " ".join(parts)


def _exclude_args(excludes: list[str] | tuple[str, ...]) -> list[str]:
    args: list[str] = []
    for pattern in excludes:
        value = pattern.strip()
        if value and "\n" not in value and "\r" not in value and "\x00" not in value:
            args.append(f"--exclude={value}")
    return args


def target_rsync_spec(*, host: str, username: str | None, target_path: str) -> str:
    """user@host:path spec (bracketed IPv6), quoted once for the shell.

    The port NEVER appears here: rsync treats everything after the first
    colon of a remote spec as path; the port travels only via build_rsync_command's
    `-e` ssh options.
    """
    host_part = f"[{host}]" if ":" in host else host
    user_part = f"{username}@" if username else ""
    spec = f"{user_part}{host_part}:{target_path}"
    return shlex.quote(spec)


def parse_progress2(chunk: str, job: TransferJob) -> None:
    """Update a job from rsync --info=progress2 output lines."""
    for line in chunk.splitlines():
        match = _PROGRESS_RE.search(line)
        if match is None:
            continue
        percent = float(match.group(1))
        total = getattr(job, "bytes_total", None)
        if isinstance(total, int) and total > 0:
            job.bytes_done = int(total * percent / 100)
        speed = re.search(r"(\d+(?:\.\d+)?[kMGT]?)B/s", line)
        if speed is not None:
            job.rate_bps = parse_bps(speed.group(1))
        eta = re.search(r"(\d+):(\d{2}):(\d{2})", line)
        if eta is not None:
            h, m, s = (int(group) for group in eta.groups())
            job.eta_s = h * 3600 + m * 60 + s


_RATE_SUFFIXES = {"k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def parse_bps(text: str) -> float:
    match = re.match(r"(\d+(?:\.\d+)?)([kMGT]?)B?", text)
    if not match:
        return 0.0
    return float(match.group(1)) * _RATE_SUFFIXES.get(match.group(2), 1)


async def plan_transfer(
    *,
    requested: TransferStrategy,
    source_server: ServerLike,
    target_server: ServerLike,
    source_executor: ExecutorLike,
    target_executor: ExecutorLike,
    source_path: str,
    target_path: str,
    artifact_id: str,
    ssh: SshManager,
    excludes: list[str] | None = None,
    probe_timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    dedicated_key: DedicatedKeyOptions | None = None,
) -> TransferPlan:
    """Run the direct-rsync preflight and select the strategy.

    Never raises for preflight failures — they produce a plan with relay
    availability and a reason; strategy selection follows AUTO rules.
    Bounded disk probes (source size, target free) run alongside; a probe
    failure leaves the plan field as None and never blocks planning.

    Direct-auth ladder (Phase 4.2C): rsync on both ends → native BatchMode
    probe → dedicated-key probe (only when the pair has a configured SGC
    dedicated key). The winning method lands in ``plan.direct_auth_method``.
    """

    plan = TransferPlan(
        artifact_id=artifact_id,
        source_server_id=source_server.server_id,  # type: ignore[attr-defined]
        source_path=source_path,
        target_server_id=target_server.server_id,  # type: ignore[attr-defined]
        target_path=target_path,
        strategy_requested=requested,
        excludes=list(excludes or ()),
    )

    plan.source_size_b = await _probe_source_size(source_executor, source_path, probe_timeout_s)
    plan.target_free_b = await _probe_target_free(target_executor, target_path, probe_timeout_s)
    if (
        plan.source_size_b is not None
        and plan.target_free_b is not None
        and plan.target_free_b < plan.source_size_b
    ):
        plan.space_warning = (
            f"target may be insufficient: need {plan.source_size_b} B,"
            f" only {plan.target_free_b} B free"
        )

    rsync_source = await _rsync_present(source_executor)
    rsync_target = await _rsync_present(target_executor)
    probe_ok = False
    probe_detail = ""
    direct_method: str | None = None
    if rsync_source and rsync_target:
        try:
            params = ssh.resolve_params_for(target_server)
            session = await ssh.transfer_command_session(source_server)
            probe_ok, probe_detail = await _batch_mode_probe(
                session, params.host, params.port, params.username
            )
            if probe_ok:
                direct_method = "native"
            elif dedicated_key is not None:
                probe_ok, probe_detail = await dedicated_batch_mode_probe(
                    session, params.host, params.port, params.username, dedicated_key
                )
                if probe_ok:
                    direct_method = "sgc_key"
            await session.close()
        except Exception as exc:  # transport failure -> relay
            probe_ok = False
            probe_detail = f"preflight probe failed: {str(exc)[:120]}"

    direct_available = bool(rsync_source and rsync_target and probe_ok)
    plan.strategy_available = {"direct_rsync": direct_available, "local_relay": True}
    plan.direct_auth_method = direct_method if direct_available else None
    if not direct_available:
        plan.direct_auth_reason = _unavailable_reason_code(rsync_source, rsync_target)

    if requested is TransferStrategy.LOCAL_RELAY:
        plan.strategy_selected = TransferStrategy.LOCAL_RELAY
        plan.reason = "local relay requested"
        return plan
    if requested is TransferStrategy.DIRECT_RSYNC:
        if direct_available:
            plan.strategy_selected = TransferStrategy.DIRECT_RSYNC
            plan.reason = f"direct rsync preflight passed ({direct_method})"
        else:
            plan.strategy_selected = None
            plan.reason = "direct rsync unavailable: " + (
                "rsync missing on source or target"
                if not (rsync_source and rsync_target)
                else (probe_detail or "source cannot SSH to target non-interactively")
            )
        return plan

    # AUTO
    if direct_available:
        plan.strategy_selected = TransferStrategy.DIRECT_RSYNC
        plan.reason = f"direct rsync preflight passed ({direct_method}, auto)"
    else:
        plan.strategy_selected = TransferStrategy.LOCAL_RELAY
        plan.reason = (
            f"direct rsync unavailable: {probe_detail}; using local relay"
            if probe_detail
            else "direct rsync unavailable; falling back to local relay"
        )
    return plan


def _unavailable_reason_code(rsync_source: bool, rsync_target: bool) -> str | None:
    """Short taxonomy code for the plan preview when direct rsync is off."""

    if not rsync_source:
        return "rsync_missing_source"
    if not rsync_target:
        return "rsync_missing_target"
    return None


async def _rsync_present(executor: ExecutorLike) -> bool:
    try:
        result = await executor.run("command -v rsync", timeout_s=10)
        return result.exit_code == 0
    except Exception:
        return False


async def _probe_source_size(
    executor: ExecutorLike, source_path: str, timeout_s: float
) -> int | None:
    """Apparent source size in bytes, one bounded round trip.

    Directories go through `du -sb`, single files through `stat -c %s`
    (dispatched remotely in one shell command to keep the preflight bounded);
    any failure, timeout, or unparseable output yields None — a probe guess
    must never block or fail a transfer.
    """
    quoted = shlex.quote(source_path)
    command = f"if [ -d {quoted} ]; then du -sb -- {quoted}; else stat -c %s -- {quoted}; fi"
    try:
        result = await executor.run(command, timeout_s=timeout_s)
        if result.exit_code != 0:
            return None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        value = int(lines[-1].split()[0])
        return value if value >= 0 else None
    except Exception:
        return None


async def _probe_target_free(
    executor: ExecutorLike, target_path: str, timeout_s: float
) -> int | None:
    """Free bytes on the filesystem holding the target's parent directory.

    `df -B1 --output=avail` last line; failure of any kind yields None.
    """
    parent = posixpath.dirname(target_path) or "."
    command = f"df -B1 --output=avail -- {shlex.quote(parent)}"
    try:
        result = await executor.run(command, timeout_s=timeout_s)
        if result.exit_code != 0:
            return None
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            return None
        return int(lines[-1].split()[-1])
    except Exception:
        return None


async def _batch_mode_probe(
    session: LongCommandSession, host: str, port: int | None, username: str | None
) -> tuple[bool, str]:
    """Strict non-interactive SSH test: no prompts, short timeout.

    StrictHostKeyChecking=yes never writes to the source server's
    known_hosts (SAFE-BY-DESIGN): an untrusted target fails the probe,
    DIRECT_RSYNC stays unavailable and AUTO falls back to LOCAL_RELAY.

    Returns (ok, detail): on failure detail carries the collected ssh stderr
    (newlines collapsed, bounded) so the plan reason can say WHY direct rsync
    is unavailable instead of a bare 'unavailable'.
    """

    user_part = f"{username}@" if username else ""
    command = (
        f"ssh -o BatchMode=yes -o ConnectTimeout=8"
        f" -o StrictHostKeyChecking=yes -p {int(port or 22)}"
        f" {shlex.quote(f'{user_part}{host}')} true"
    )
    stderr_chunks: list[str] = []

    async def _collect_stderr(chunk: str) -> None:
        stderr_chunks.append(chunk)

    try:
        exit_code = await session.run(command, timeout_s=20.0, on_stderr=_collect_stderr)
    except Exception as exc:
        return False, f"probe error: {str(exc)[:120]}"
    if exit_code == 0:
        return True, ""
    return False, _probe_detail("".join(stderr_chunks), exit_code)


def _probe_detail(stderr: str, exit_code: int) -> str:
    """Collected probe stderr as a one-line reason detail (~160 chars max).

    Newlines collapse to '; ' so the reason stays single-line; an empty
    stderr degrades to the exit code so the reason is never bare.
    """
    collapsed = "; ".join(line.strip() for line in stderr.splitlines() if line.strip())
    detail = collapsed[:_PROBE_DETAIL_MAX_CHARS].strip()
    return detail or f"ssh exited with code {exit_code}"


async def dedicated_batch_mode_probe(
    session: LongCommandSession,
    host: str,
    port: int | None,
    username: str | None,
    options: DedicatedKeyOptions,
) -> tuple[bool, str]:
    """BatchMode probe through the SGC dedicated transfer key (Phase 4.2C).

    Same strictness as the native probe, plus the dedicated identity and the
    app-owned known_hosts on the source: `-i <key> -o IdentitiesOnly=yes
    -o UserKnownHostsFile=<sgc-known-hosts>`. Never a password, never agent
    forwarding, never a weaker host-key mode.
    """

    user_part = f"{username}@" if username else ""
    command = (
        f"ssh -i {shlex.quote(options.private_key_path)}"
        f" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
        f" -o UserKnownHostsFile={shlex.quote(options.known_hosts_path)}"
        f" -o ConnectTimeout=8 -p {int(port or 22)}"
        f" {shlex.quote(f'{user_part}{host}')} true"
    )
    stderr_chunks: list[str] = []

    async def _collect_stderr(chunk: str) -> None:
        stderr_chunks.append(chunk)

    try:
        exit_code = await session.run(command, timeout_s=20.0, on_stderr=_collect_stderr)
    except Exception as exc:
        return False, f"probe error: {str(exc)[:120]}"
    if exit_code == 0:
        return True, ""
    return False, _probe_detail("".join(stderr_chunks), exit_code)

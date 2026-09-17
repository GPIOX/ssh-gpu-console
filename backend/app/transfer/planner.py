"""Transfer planning: DIRECT_RSYNC preflight + AUTO fallback (Sol contract).

Direct rsync is selected only when EVERY condition holds: rsync on both ends,
strict non-interactive source→target SSH (BatchMode), no credential copying.
Any preflight failure falls back to Local Relay without failing the transfer.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from app.collectors.base import ExecutorLike
from app.models.transfer import TransferJob, TransferPlan, TransferStrategy
from app.ssh.file_transfer import ServerLike

if TYPE_CHECKING:
    from app.ssh.manager import SshManager
    from app.ssh.transport import LongCommandSession

_RSYNC_CHECK = "command -v rsync"
_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def build_rsync_command(
    *,
    source_path: str,
    target_spec: str,
    target_port: int | None,
    excludes: list[str] | tuple[str, ...] = (),
) -> str:
    """Fixed-flag rsync command with safely quoted arguments (no user flags).

    Resume via --partial/--partial-dir; never --delete; never --append.
    Exclusion patterns travel as --exclude=arg (full rsync semantics; the
    transfer root itself is never excluded by rsync).
    """
    ssh_opts = "ssh -o BatchMode=yes -o ConnectTimeout=8"
    if target_port:
        ssh_opts += f" -p {int(target_port)}"
    parts = [
        "rsync",
        "-a",
        "-r",
        "-t",
        "-p",
        "-o",
        "-g",
        "--partial",
        "--partial-dir=.sgc-rsync-partial",
        "--info=progress2",
        "-e",
        shlex.quote(ssh_opts),
        *(_exclude_args(excludes)),
        "--",
        shlex.quote(source_path),
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


def target_rsync_spec(
    *, host: str, port: int | None, username: str | None, target_path: str
) -> str:
    """user@host:port:path spec (bracketed IPv6), quoted once for the shell."""
    host_part = f"[{host}]" if ":" in host else host
    user_part = f"{username}@" if username else ""
    port_part = f":{int(port)}" if port and port != 22 else ""
    spec = f"{user_part}{host_part}{port_part}:{target_path}"
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
) -> TransferPlan:
    """Run the direct-rsync preflight and select the strategy.

    Never raises for preflight failures — they produce a plan with relay
    availability and a reason; strategy selection follows AUTO rules.
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

    rsync_source = await _rsync_present(source_executor)
    rsync_target = await _rsync_present(target_executor)
    probe_ok = False
    probe_note = ""
    if rsync_source and rsync_target:
        try:
            params = ssh.resolve_params_for(target_server)
            session = await ssh.transfer_command_session(source_server)
            probe_ok = await _batch_mode_probe(session, params.host, params.port, params.username)
            await session.close()
        except Exception as exc:  # transport failure -> relay
            probe_ok = False
            probe_note = f"preflight probe failed: {str(exc)[:120]}"

    direct_available = bool(rsync_source and rsync_target and probe_ok)
    plan.strategy_available = {"direct_rsync": direct_available, "local_relay": True}

    if requested is TransferStrategy.LOCAL_RELAY:
        plan.strategy_selected = TransferStrategy.LOCAL_RELAY
        plan.reason = "local relay requested"
        return plan
    if requested is TransferStrategy.DIRECT_RSYNC:
        if direct_available:
            plan.strategy_selected = TransferStrategy.DIRECT_RSYNC
            plan.reason = "direct rsync preflight passed"
        else:
            plan.strategy_selected = None
            plan.reason = "direct rsync unavailable: " + (
                "rsync missing on source or target"
                if not (rsync_source and rsync_target)
                else (probe_note or "source cannot SSH to target non-interactively")
            )
        return plan

    # AUTO
    if direct_available:
        plan.strategy_selected = TransferStrategy.DIRECT_RSYNC
        plan.reason = "direct rsync preflight passed (auto)"
    else:
        plan.strategy_selected = TransferStrategy.LOCAL_RELAY
        plan.reason = probe_note or ("direct rsync unavailable; falling back to local relay")
    return plan


async def _rsync_present(executor: ExecutorLike) -> bool:
    try:
        result = await executor.run("command -v rsync", timeout_s=10)
        return result.exit_code == 0
    except Exception:
        return False


async def _batch_mode_probe(
    session: LongCommandSession, host: str, port: int | None, username: str | None
) -> bool:
    """Strict non-interactive SSH test: no prompts, short timeout."""

    user_part = f"{username}@" if username else ""
    command = (
        f"ssh -o BatchMode=yes -o ConnectTimeout=8"
        f" -o StrictHostKeyChecking=accept-new -p {int(port or 22)}"
        f" {shlex.quote(f'{user_part}{host}')} true"
    )
    try:
        return await session.run(command, timeout_s=20.0) == 0
    except Exception:
        return False

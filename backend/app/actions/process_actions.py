"""Named process actions — the only write path to remote servers.

Security invariants:
- the remote command comes exclusively from `kill_spec` (allowlisted signal,
  validated PID); no user string is ever interpolated;
- the target server must exist and be enabled; the API layer enforces that;
- outcomes are classified (`not_found`, `permission_denied`) so the UI can
  speak precisely instead of dumping stderr.
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.models.server import ActionResult, ServerRecord
from app.ssh.commands import kill_spec
from app.ssh.executor import Executor, ExecutorError

logger = get_logger("actions.process")

_RC_RE = re.compile(r"rc=(\d+)\s*$")

_ACTION_BY_SIGNAL = {"TERM": "terminate_process", "KILL": "kill_process"}


def _classify_output(exit_code: int, output: str) -> tuple[str, str]:
    lowered = output.lower()
    if exit_code == 0 and "rc=0" in lowered:
        return "ok", "signal delivered"
    if "no such process" in lowered:
        return "not_found", "process does not exist (it may have exited)"
    if "operation not permitted" in lowered:
        return "permission_denied", "permission denied for this user on the remote host"
    if exit_code != 0:
        detail = output.strip() or f"exit code {exit_code}"
        return "failed", detail[:300]
    return "ok", output.strip()[:300]


async def run_process_action(
    executor: Executor, record: ServerRecord, *, signal: str, pid: int
) -> ActionResult:
    action = _ACTION_BY_SIGNAL[signal]
    spec = kill_spec(signal, pid)  # validates pid + allowlists the signal
    try:
        result = await executor.run(spec.command, timeout_s=spec.timeout_s)
    except ExecutorError as exc:
        logger.info("action %s pid=%d on %s: transport %s", action, pid, record.server_id, exc.code)
        return ActionResult(
            action=action, pid=pid, ok=False, status=exc.code, detail=exc.detail[:300]
        )
    match = _RC_RE.search(result.stdout)
    exit_code = int(match.group(1)) if match else result.exit_code
    status, detail = _classify_output(exit_code, result.stdout)
    logger.info("action %s pid=%d on %s: %s", action, pid, record.server_id, status)
    return ActionResult(action=action, pid=pid, ok=status == "ok", status=status, detail=detail)

"""Named action routes. There is no shell endpoint anywhere in this API.

Only two write actions exist for the MVP: terminate (SIGTERM) and kill
(SIGKILL) a process, each taking nothing but a validated PID for an explicit,
enabled server.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.actions.process_actions import run_process_action
from app.core.errors import AppError
from app.models.server import ActionResult, ProcessActionRequest
from app.runtime import get_runtime
from app.servers.registry import get_default_registry
from app.ssh.transport import build_executor

router = APIRouter(prefix="/api/v1", tags=["actions"])


async def _run(server_id: str, signal: str, pid: int) -> ActionResult:
    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code, "message": error.message}
        ) from error
    if not server.enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "server_disabled", "message": "server is disabled"},
        )
    executor = build_executor(get_runtime().ssh, server)
    return await run_process_action(executor, server, signal=signal, pid=pid)


@router.post("/servers/{server_id}/actions/terminate-process", response_model=ActionResult)
async def terminate_process(server_id: str, request: ProcessActionRequest) -> ActionResult:
    return await _run(server_id, "TERM", request.pid)


@router.post("/servers/{server_id}/actions/kill-process", response_model=ActionResult)
async def kill_process(server_id: str, request: ProcessActionRequest) -> ActionResult:
    return await _run(server_id, "KILL", request.pid)

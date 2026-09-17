"""Thin server registry / onboarding routes under /api/v1.

Routes resolve collaborators through ``app.runtime.get_runtime()`` (manager)
and the registry/discovery accessors; they never run remote commands beyond
the manager's explicit connection test.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.core.errors import AppError
from app.core.lifecycle import notify_registry_changed
from app.models.server import (
    AliasEntry,
    ConnectionTestResult,
    HostKeyPrompt,
    ServerCreate,
    ServerPatch,
    ServerRecord,
)
from app.models.telemetry import ServerStatus
from app.runtime import get_runtime
from app.servers.discovery import get_default_discovery
from app.servers.registry import get_default_registry

router = APIRouter(prefix="/api/v1", tags=["servers"])


def _http_error(error: AppError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get("/servers", response_model=list[ServerRecord])
def list_servers() -> list[ServerRecord]:
    return get_default_registry().all()


@router.post("/servers", response_model=ServerRecord, status_code=status.HTTP_201_CREATED)
async def create_server(request: ServerCreate) -> ServerRecord:
    try:
        record = get_default_registry().create(request)
    except AppError as error:
        raise _http_error(error) from error
    await notify_registry_changed()
    return record


@router.get("/servers/ssh-config/aliases", response_model=list[AliasEntry])
def list_ssh_aliases() -> list[AliasEntry]:
    discovery = get_default_discovery()
    discovery.scan()
    return [
        AliasEntry(alias=item.alias, host=item.hostname, user=item.username, port=item.port)
        for item in discovery.aliases()
        if not item.ignored
    ]


@router.patch("/servers/{server_id}", response_model=ServerRecord)
async def update_server(server_id: str, request: ServerPatch) -> ServerRecord:
    registry = get_default_registry()
    try:
        previous = registry.get(server_id)
        updated = registry.update(server_id, request)
    except AppError as error:
        raise _http_error(error) from error

    identity_changed = (previous.ssh_host, previous.username, previous.port) != (
        updated.ssh_host,
        updated.username,
        updated.port,
    )
    disabled = previous.enabled and not updated.enabled
    if identity_changed or disabled:
        await get_runtime().ssh.close_server(server_id)
    await notify_registry_changed()
    return updated


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: str) -> Response:
    try:
        get_default_registry().delete(server_id)
    except AppError as error:
        raise _http_error(error) from error
    await get_runtime().ssh.close_server(server_id)
    await notify_registry_changed()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/servers/{server_id}/test", response_model=ConnectionTestResult)
async def test_server(server_id: str) -> ConnectionTestResult:
    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    if not server.enabled:
        return ConnectionTestResult(
            ok=False, status=ServerStatus.UNKNOWN.value, detail="server is disabled"
        )
    return await get_runtime().ssh.test_connection(server)


@router.post("/servers/{server_id}/host-key/trust", response_model=ConnectionTestResult)
async def trust_host_key(server_id: str, prompt: HostKeyPrompt) -> ConnectionTestResult:
    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    return await get_runtime().ssh.trust_host_key(server, prompt)

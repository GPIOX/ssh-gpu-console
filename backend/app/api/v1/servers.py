"""Thin server registry / onboarding routes under /api/v1.

Routes resolve collaborators through ``app.runtime.get_runtime()`` (manager)
and the registry/discovery/credential accessors; they never run remote
commands beyond the manager's explicit connection test. Password credentials
are handled through the credential store only: the PUT body (a SecretStr) is
never echoed, logged, or persisted to JSON.
"""

from __future__ import annotations

import contextlib
import os
import stat
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Response, status

from app.core.errors import AppError
from app.core.lifecycle import notify_registry_changed
from app.core.logging import get_logger
from app.credentials.store import get_credential_store
from app.models.credentials import AuthStatus, PasswordCredentialStatus, ServerPasswordSet
from app.models.direct_auth import DirectAuthList, DirectAuthStatus
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

if TYPE_CHECKING:
    from app.direct_auth.service import DirectAuthService

router = APIRouter(prefix="/api/v1", tags=["servers"])

logger = get_logger("api.servers")


def _http_error(error: AppError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


def _direct_auth() -> DirectAuthService:
    service = get_runtime().direct_auth
    if service is None:
        raise RuntimeError("direct-auth service unavailable")
    return service


def _agent_available() -> bool:
    """True when SSH_AUTH_SOCK points at an existing socket (stat only — no connect)."""

    sock_path = os.environ.get("SSH_AUTH_SOCK")
    if not sock_path:
        return False
    try:
        mode = os.stat(sock_path).st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


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


@router.get("/servers/{server_id}/auth", response_model=AuthStatus)
def get_auth_status(server_id: str) -> AuthStatus:
    """Local auth posture for one server (counts and booleans only)."""

    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    params = get_runtime().ssh.resolve_params_for(server)
    store = get_credential_store()
    configured = store.has_password(server_id)
    return AuthStatus(
        ssh_config_used=params.source == "ssh_config",
        effective_host=params.host,
        effective_user=params.username,
        effective_port=params.port,
        identity_files=len(params.identity_files),
        agent_available=_agent_available(),
        proxy_jump_configured=params.proxy_jump is not None,
        password_configured=configured,
        password_storage=store.storage_mode().value if configured else None,
    )


@router.put(
    "/servers/{server_id}/credentials/password",
    response_model=PasswordCredentialStatus,
)
async def set_server_password(
    server_id: str, request: ServerPasswordSet
) -> PasswordCredentialStatus:
    try:
        get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    store = get_credential_store()
    try:
        store.set_password(server_id, request.password)
    except Exception:
        # Never echo the body or the underlying error (the secret must not
        # reach the response, logs or exception messages).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "password credential could not be stored"},
        ) from None
    # Retire the current connection so the next use reconnects with the new
    # credential (telemetry/transfer paths reconnect transparently).
    await get_runtime().ssh.close_server(server_id)
    return PasswordCredentialStatus(configured=True, storage=store.storage_mode().value)


@router.delete(
    "/servers/{server_id}/credentials/password",
    response_model=PasswordCredentialStatus,
)
async def delete_server_password(server_id: str) -> PasswordCredentialStatus:
    try:
        get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    store = get_credential_store()
    try:
        store.delete_password(server_id)  # absent id: a fine no-op
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "conflict", "message": "password credential could not be removed"},
        ) from None
    await get_runtime().ssh.close_server(server_id)
    return PasswordCredentialStatus(configured=False, storage=None)


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
    # Best-effort credential cleanup: the deletion itself must succeed even
    # when the keyring cannot (logged WITHOUT any secret material).
    with contextlib.suppress(Exception):
        try:
            store = get_credential_store()
            if store.has_password(server_id):
                store.delete_password(server_id)
        except Exception:
            logger.warning("password credential cleanup failed for server %s (ignored)", server_id)
    # Local direct-auth bookkeeping only: drop the metadata rows mentioning
    # this server (NEVER a remote revoke from a deletion — remote materials
    # stay; a re-added server simply has no configured pair).
    with contextlib.suppress(Exception):
        _direct_auth().forget_server(server_id)
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


# ---- server→server direct transfer auth (Phase 4.2C) ---------------------------------
# LOCAL auth (console→server) and DIRECT auth (source→target for DIRECT_RSYNC)
# are separate planes; a password is never used for direct rsync.


@router.get("/servers/{server_id}/direct-auth", response_model=DirectAuthList)
def list_direct_auth(server_id: str) -> DirectAuthList:
    """Zero-SSH view of the configured direct-auth pairs of one server."""
    try:
        get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    return _direct_auth().list_for(server_id)


@router.post(
    "/servers/{source_id}/direct-auth/{target_id}/check",
    response_model=DirectAuthStatus,
)
async def check_direct_auth(source_id: str, target_id: str) -> DirectAuthStatus:
    """Explicit, bounded SSH check of the source→target direct-auth ladder."""
    try:
        return await _direct_auth().check(source_id, target_id)
    except AppError as error:
        raise _http_error(error) from error


@router.post(
    "/servers/{source_id}/direct-auth/{target_id}/setup-key",
    response_model=DirectAuthStatus,
)
async def setup_direct_auth_key(source_id: str, target_id: str) -> DirectAuthStatus:
    """Install the SGC dedicated transfer key (explicit user click; item 27)."""
    try:
        return await _direct_auth().setup_key(source_id, target_id)
    except AppError as error:
        raise _http_error(error) from error


@router.delete("/servers/{source_id}/direct-auth/{target_id}")
async def revoke_direct_auth_key(source_id: str, target_id: str) -> dict[str, bool]:
    """Revoke the pair's dedicated key materials; never claims configured."""
    try:
        await _direct_auth().revoke(source_id, target_id)
    except AppError as error:
        raise _http_error(error) from error
    return {"configured": False}


@router.post("/servers/{server_id}/host-key/trust", response_model=ConnectionTestResult)
async def trust_host_key(server_id: str, prompt: HostKeyPrompt) -> ConnectionTestResult:
    try:
        server = get_default_registry().get(server_id)
    except AppError as error:
        raise _http_error(error) from error
    return await get_runtime().ssh.trust_host_key(server, prompt)

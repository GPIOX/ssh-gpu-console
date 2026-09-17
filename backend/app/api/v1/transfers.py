"""Transfer routes: list/detail (RAM), plan, create, cancel, retry.

Listing NEVER triggers SSH. Progress is polled at ~1 Hz by the frontend only
while jobs are active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.routing import APIRoute

from app.core.errors import AppError
from app.models.transfer import (
    TransferBatch,
    TransferJob,
    TransferPlan,
    TransferRequest,
    TransferState,
)
from app.runtime import get_runtime

if TYPE_CHECKING:
    from app.transfer.batch import BatchRegistry
    from app.transfer.service import TransferService

router = APIRouter(prefix="/api/v1/transfers", tags=["transfers"])


def _service() -> TransferService:
    service = get_runtime().transfers
    if service is None:
        raise RuntimeError("transfer service unavailable")
    return service


def _http_error(error: AppError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@router.get("", response_model=list[TransferJob])
def list_transfers() -> list[TransferJob]:
    return _service().list_jobs()


@router.get("/{job_id}", response_model=TransferJob)
def get_transfer(job_id: str) -> TransferJob:
    try:
        return _service().job(job_id)  # type: ignore[return-value]
    except AppError as error:
        raise _http_error(error) from error


@router.post("/plan", response_model=TransferPlan)
async def plan_transfer(request: TransferRequest) -> TransferPlan:
    try:
        return await _service().plan(request)
    except AppError as error:
        raise _http_error(error) from error


@router.post("", status_code=202)
async def create_transfer(request: TransferRequest) -> dict[str, str]:
    try:
        # Must run ON the event loop: create() schedules the worker via
        # asyncio.create_task (a sync def endpoint would execute in the
        # threadpool, where no loop runs — RuntimeError → 500).
        return _service().create(request)
    except AppError as error:
        raise _http_error(error) from error


@router.delete("", status_code=200)
async def clear_transfer_history() -> dict[str, int]:
    return {"cleared": _service().clear_history()}


@router.post("/{job_id}/cancel", status_code=200)
async def cancel_transfer(job_id: str) -> dict[str, str]:
    try:
        _service().cancel(job_id)
        return {"job_id": job_id, "state": "cancelling"}
    except AppError as error:
        raise _http_error(error) from error


@router.post("/{job_id}/retry", status_code=202)
async def retry_transfer(job_id: str) -> dict[str, str]:
    try:
        return await _service().retry(job_id)
    except AppError as error:
        raise _http_error(error) from error


# ---- transfer batches (Phase 4D: RAM-only grouping of sync jobs) ------------------


def _batches() -> BatchRegistry:
    registry = get_runtime().batches
    if registry is None:
        raise RuntimeError("transfer batch registry unavailable")
    return registry


def _job_states() -> dict[str, TransferState]:
    """Current state of every known job; feeds the batch state derivation."""
    return {job.job_id: job.state for job in _service().list_jobs()}


async def _list_transfer_batches() -> list[TransferBatch]:
    # RAM only, zero SSH: active + archived batches, newest first.
    return _batches().list(_job_states())


async def _get_transfer_batch(batch_id: str) -> TransferBatch:
    try:
        return _batches().get(batch_id, _job_states())
    except AppError as error:
        raise _http_error(error) from error


# Contract paths are /api/v1/transfer-batches[-/{batch_id}]; the transfers
# router carries the /api/v1/transfers prefix and include_router would
# concatenate it, so the two routes are registered with absolute paths.
router.routes.append(
    APIRoute(
        "/api/v1/transfer-batches",
        _list_transfer_batches,
        methods=["GET"],
        response_model=list[TransferBatch],
        name="list_transfer_batches",
    )
)
router.routes.append(
    APIRoute(
        "/api/v1/transfer-batches/{batch_id}",
        _get_transfer_batch,
        methods=["GET"],
        response_model=TransferBatch,
        name="get_transfer_batch",
    )
)

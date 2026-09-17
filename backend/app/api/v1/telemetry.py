"""Read-only telemetry REST endpoints.

The scheduler owns collection; these routes only read the shared in-memory
state through the TelemetryService facade, so they can never trigger an SSH
command per browser request.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.models.telemetry import FleetSummary, HistoryPoint, ServerSnapshot
from app.runtime import get_runtime
from app.telemetry.history import GPU_METRICS

router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


@router.get("/fleet", response_model=FleetSummary)
def fleet_summary() -> FleetSummary:
    return get_runtime().telemetry.fleet_summary()


@router.get("/servers/{server_id}", response_model=ServerSnapshot)
def server_snapshot(server_id: str) -> ServerSnapshot:
    snapshot = get_runtime().telemetry.snapshot(server_id)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "server not found"},
        )
    return snapshot


@router.get("/servers/{server_id}/gpu-history", response_model=list[HistoryPoint])
def gpu_history(server_id: str, gpu: int = 0, metric: str = "utilization") -> list[HistoryPoint]:
    if metric not in GPU_METRICS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "bad_metric",
                "message": f"metric must be one of: {', '.join(GPU_METRICS)}",
            },
        )
    telemetry = get_runtime().telemetry
    if not telemetry.has_server(server_id):
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "server not found"},
        )
    try:
        return telemetry.gpu_history(server_id, gpu, metric)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_request", "message": str(error)},
        ) from error

"""Composition root: FastAPI app, lifespan, health, and static frontend.

Production shape: one uvicorn process serving the API and the built Vite
output. No Node runtime is required.
"""

from __future__ import annotations

import asyncio
import resource
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request

from app.api.v1 import actions, realtime, servers, telemetry, transfers, workspace
from app.core.config import Settings
from app.core.lifecycle import AppContext, build_context, install_runtime
from app.core.logging import get_logger
from app.runtime import set_runtime

logger = get_logger("main")

_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def create_app(
    settings: Settings | None = None,
    *,
    context: AppContext | None = None,
) -> FastAPI:
    """Build the application. `context` is injectable for tests."""

    settings = settings or Settings()
    app_context = context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal app_context
        if app_context is None:
            app_context = build_context(settings)
        install_runtime(app_context)
        await app_context.start()
        try:
            yield
        finally:
            await app_context.stop()
            set_runtime(None)

    app = FastAPI(title="SSH GPU Console", version="3.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(servers.router)
    app.include_router(telemetry.router)
    app.include_router(realtime.router)
    app.include_router(actions.router)
    app.include_router(workspace.router)
    app.include_router(transfers.router)

    from app.core.errors import AppError

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, error: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "message": error.message},
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        """Resource/telemetry self-report for the resource audit."""
        telemetry = getattr(app_context, "telemetry", None)
        ssh = getattr(app_context, "ssh", None)
        scheduler = getattr(telemetry, "_scheduler", None)
        stats: object = None
        if ssh is not None:
            connection_stats = getattr(ssh, "connection_stats", None)
            if callable(connection_stats):
                stats = await connection_stats()
        return {
            "status": "ok",
            "rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "asyncio_tasks": len(asyncio.all_tasks()),
            "scheduler_mode": getattr(scheduler, "mode", lambda: "unknown")(),
            "realtime_clients": getattr(telemetry, "client_count", 0),
            "connections": stats,
        }

    if _FRONTEND_DIST.is_dir():
        _mount_frontend(app, _FRONTEND_DIST)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA. Any non-API path resolves to index.html, and the
    resolved file must stay inside dist (containment check)."""

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if ".." in full_path:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="not found")
        candidate = (dist / full_path).resolve() if full_path else dist / "index.html"
        if full_path and candidate.is_file() and dist in candidate.parents:
            return FileResponse(candidate)
        index = dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="frontend build not found")


app = create_app()

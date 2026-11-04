"""FastAPI application factory.

In production the built SPA is served by this same process from ``voxarium_studio/static`` —
one process, one port, equally simple locally and on a server (§2.5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .build_info import build_info
from .core.context import AppContext
from .core.security import AuthSettings
from .core.workspace import initialise_workspace, project_label, record_recent, shutdown_workspace
from .ws.routes import router as ws_router

log = logging.getLogger(__name__)


def static_dir() -> Path | None:
    """Built frontend: packaged next to the module, or the Vite output in a source tree."""
    package_dir = Path(__file__).resolve().parent
    for candidate in (
        package_dir / "static",
        package_dir.parent.parent / "frontend" / "dist",
    ):
        if (candidate / "index.html").exists():
            return candidate
    return None


def create_app(
    project_root: str,
    *,
    auth: AuthSettings | None = None,
    dev_origins: list[str] | None = None,
) -> FastAPI:
    auth = auth or AuthSettings()

    # Resolve the build here, off the event loop: in a source tree the first call shells
    # out to git, and that is not something a request should be waiting on.
    build = build_info()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = AppContext.create(project_root, auth)
        app.state.ctx = ctx
        # Kept for the restart an update ends in: coming back up without `--dev` would
        # leave the browser on Vite's port talking to a backend that refuses it (§12.1).
        app.state.dev_origins = list(dev_origins or [])
        # Opening another project replaces `app.state.ctx` wholesale; the lock is what
        # stops two clicks on "Open project" from building two contexts over each other.
        app.state.project_lock = asyncio.Lock()
        initialise_workspace(app.state)
        await ctx.startup()
        record_recent(ctx.paths.root, project_label(ctx))
        try:
            yield
        finally:
            await shutdown_workspace(app.state)

    app = FastAPI(
        title="Voxarium Studio",
        version=build.version,
        description=(
            "Local web hub for AI game-dev pipelines, plugins and agent teams. "
            "All project data lives in the managed project's voxarium_studio/ directory."
        ),
        lifespan=lifespan,
    )

    if dev_origins:
        # Only in dev, where Vite serves the SPA from its own port with HMR (§2.5).
        app.add_middleware(
            CORSMiddleware,
            allow_origins=dev_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router)
    app.include_router(ws_router)

    @app.exception_handler(PermissionError)
    async def permission_handler(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        # §2.5, §2.7: the normalized release tag must match the SPA and desktop.
        # Untagged builds use their short commit; build keeps the full display label.
        return {
            "status": "ok",
            "service": "voxarium_studio",
            # Identifies the Electron-owned process, never an authentication secret (§2.7).
            "desktop_instance": os.environ.get("VOXARIUM_STUDIO_DESKTOP_INSTANCE"),
            "version": build.version,
            "build": build.display,
            "commit": build.commit,
        }

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    directory = static_dir()
    if directory is None:
        log.warning(
            "No built frontend found. Run `npm run build` in frontend/, or use the dev "
            "server with `voxarium_studio run <project> --dev`."
        )

        @app.get("/", include_in_schema=False)
        async def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "Frontend is not built. Run `npm install && npm run build` "
                    "in frontend/, or start the Vite dev server."
                },
            )

        return

    if (directory / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=directory / "assets"), name="assets")

    index_file = directory / "index.html"
    # The bundles under /assets carry a content hash in their names and may be cached for
    # ever; index.html is what points at the current ones and must not be. Without this a
    # browser is free to reuse a heuristically cached index after `run.cmd -Build`, and a
    # reload then shows the previous SPA against the new backend.
    no_cache = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(index_file, headers=no_cache)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> FileResponse:
        """Serve real files, otherwise hand the route to the SPA router."""
        candidate = (directory / full_path).resolve()
        if directory in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file, headers=no_cache)

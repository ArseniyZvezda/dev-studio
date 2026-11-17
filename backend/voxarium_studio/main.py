"""FastAPI application factory.

In production the built SPA is served by this same process from ``voxarium_studio/static`` —
one process, one port, equally simple locally and on a server (§2.5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.routing import BaseRoute, Match

from .api import api_router
from .api.project import update_application_settings, update_project_settings, update_settings
from .api.remote import create_remote_router
from .build_info import build_info
from .core.context import AppContext
from .core.security import AuthSettings
from .core.workspace import initialise_workspace, project_label, record_recent, shutdown_workspace
from .sync.catalog import CatalogError
from .sync.catalog_runtime import RemoteCatalogReady, RemoteCatalogRuntime, create_remote_catalog_runtime
from .sync.reservations import ReservationOwner
from .ws.bus import EventBus
from .ws.routes import router as ws_router

if TYPE_CHECKING:
    from pydantic import SecretStr
    from .api.remote import ServerProfile
    from .core.application_config import RemoteServerProfile
    from .sync.catalog import CatalogPage, CatalogProject, CreateResult
    from .sync.catalog_reads import LegacyResolution, ProjectSnapshot
    from .sync.catalog_verification import VerificationStatus

log = logging.getLogger(__name__)


class _RemoteCatalogHolder:
    """App-local admission seam; never resolves the selected project (§5.2.1).

    The router is mounted before startup, but dependencies are usable only while
    this lifespan's owner is ready. No import-level router or owner is mutated.
    """

    def __init__(self) -> None:
        self.runtime: RemoteCatalogRuntime | None = None

    @property
    def ready(self) -> RemoteCatalogReady:
        if self.runtime is None:
            raise CatalogError("service_unavailable")
        return self.runtime.ready

    def list(self) -> tuple[ServerProfile, ...]:
        return self.ready.metadata.list()

    def get(self, profile_id: str) -> RemoteServerProfile:
        return self.ready.profiles.get(profile_id)

    def create(self, name: str, url: str) -> ServerProfile:
        return self.ready.mutations.create(name, url)

    def rename(self, profile_id: str, name: str, *, expected_revision: int) -> ServerProfile:
        return self.ready.mutations.rename(profile_id, name, expected_revision=expected_revision)

    def change_url(self, profile_id: str, url: str, *, expected_revision: int,
                   clear_credential: bool) -> ServerProfile:
        return self.ready.mutations.change_url(profile_id, url, expected_revision=expected_revision,
                                                clear_credential=clear_credential)

    def set_credential(self, profile_id: str, token: SecretStr, *, expected_revision: int) -> ServerProfile:
        return self.ready.mutations.set_credential(profile_id, token, expected_revision=expected_revision)

    def clear_credential(self, profile_id: str, *, expected_revision: int) -> ServerProfile:
        return self.ready.mutations.clear_credential(profile_id, expected_revision=expected_revision)

    def delete(self, profile_id: str, *, expected_revision: int) -> None:
        self.ready.mutations.delete(profile_id, expected_revision=expected_revision)

    def recover(self, profile_id: str, *, expected_revision: int) -> ServerProfile | None:
        return self.ready.mutations.recover(profile_id, expected_revision=expected_revision)

    async def verify(self, profile_id: str, *, expected_revision: int) -> VerificationStatus:
        return await self.ready.verification.verify(profile_id, expected_revision=expected_revision)

    async def list_projects(self, profile_id: str, *, expected_revision: int,
                            after: str | None = None, limit: int = 20) -> CatalogPage:
        return await self.ready.catalog.list_projects(
            profile_id, expected_revision=expected_revision, after=after, limit=limit,
        )

    # I9 backend facades for the project flow (Select/Preview/migration). They
    # are NOT browser URL handlers: no route is mounted for them, and callers
    # reach them through app.state.remote_catalog, never app.state.ctx.
    async def get_project(self, profile_id: str, id: str, *,
                          expected_revision: int) -> CatalogProject:
        return await self.ready.catalog.get_project(
            profile_id, id, expected_revision=expected_revision,
        )

    async def snapshot(self, profile_id: str, id: str, *, expected_revision: int,
                       expected_epoch: str | None = None, limit: int = 20,
                       max_pages: int = 100, max_bytes: int = 64 * 1024 * 1024) -> ProjectSnapshot:
        return await self.ready.catalog.snapshot(
            profile_id, id, expected_revision=expected_revision, expected_epoch=expected_epoch,
            limit=limit, max_pages=max_pages, max_bytes=max_bytes,
        )

    async def resolve_legacy(self, profile_id: str, name: str, *,
                             expected_revision: int) -> LegacyResolution:
        return await self.ready.catalog.resolve_legacy(
            profile_id, name, expected_revision=expected_revision,
        )

    async def select_observation(self, profile_id: str, id: str, *,
                                 expected_revision: int) -> dict[str, object]:
        return await self.ready.catalog.select_observation(
            profile_id, id, expected_revision=expected_revision,
        )

    async def create_project(self, profile_id: str, *, expected_revision: int,
                             id: str, display_name: str, operation_id: str) -> CreateResult:
        # C9 delegates to the same C8 owner for the entire application lifespan.
        return await self.ready.create.create_project(
            profile_id, expected_revision=expected_revision, id=id,
            display_name=display_name, operation_id=operation_id,
        )

    async def recover_create(self, profile_id: str, operation_id: str, *,
                             expected_revision: int) -> CreateResult:
        return await self.ready.create.recover_create(
            profile_id, operation_id, expected_revision=expected_revision,
        )


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
    remote_catalog = _RemoteCatalogHolder()

    # Resolve the build here, off the event loop: in a source tree the first call shells
    # out to git, and that is not something a request should be waiting on.
    build = build_info()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # A failed previous lifespan must be cleaned up explicitly, never hidden
        # behind fresh admission/owners (runtime-integration-plan §3.1/I2).
        # Clean lifespans may reuse the app.
        if (getattr(app.state, "project_contexts", {})
                or getattr(app.state, "_failed_project_contexts", {})):
            raise RuntimeError("Workspace cleanup is incomplete")
        reservations = ReservationOwner()
        app.state.remote_reservations = reservations
        app.state._failed_project_contexts = {}
        app.state.project_lock = asyncio.Lock()
        # C3/V0: one fresh application owner per lifespan, not per project/tab.
        # Construction does no I/O; startup pins the real global user-config home.
        runtime = create_remote_catalog_runtime()
        remote_catalog.runtime = runtime
        # C5: shared by every starting/resident context, never selected ctx inventory.
        app.state.remote_profile_lifecycle = runtime.lifecycle
        failed = False
        try:
            await runtime.start()
            ctx = AppContext.create(project_root, auth)
            ctx.remote_reservations = reservations
            ctx.remote_profile_lifecycle = runtime.lifecycle
            app.state.ctx = ctx
            # Preserve legacy startup-event replay without publishing a project identity
            # until startup succeeds (§5.1, §5.3.2).
            browser_bus = EventBus()
            ctx.bus.forward_to = browser_bus
            try:
                runtime.lifecycle.register_context(ctx)
                await ctx.startup()
                initialise_workspace(app.state, browser_bus=browser_bus)
                record_recent(ctx.paths.root, project_label(ctx))
            except BaseException:
                # Fence admission before awaiting failed startup cleanup. Keep
                # unpublished contexts retryable without inventing a descriptor.
                reservations.close_admission()
                app.state._failed_project_contexts[ctx.paths.root] = ctx
                try:
                    await shutdown_workspace(app.state)
                except BaseException:
                    log.exception("Could not clean up failed workspace startup")
                raise
            try:
                yield
            finally:
                # Stop Remote admission before closing any application resources.
                remote_catalog.runtime = None
                runtime.lifecycle.close_admission()
                await shutdown_workspace(app.state)
        except BaseException:
            reservations.close_admission()
            failed = True
            raise
        finally:
            remote_catalog.runtime = None
            try:
                await runtime.close()
            except BaseException:
                if not failed:
                    raise
                log.exception("Could not clean up failed Remote catalog lifespan")

    app = FastAPI(
        title="Voxarium Studio",
        version=build.version,
        description=(
            "Local web hub for AI game-dev pipelines, plugins and agent teams. "
            "All project data lives in the managed project's .voxarium_studio/ directory."
        ),
        lifespan=lifespan,
    )
    # Server policy must exist even before readiness/first request (§5.3).
    app.state.auth = auth
    # Also used by the update relauncher to preserve --dev (§12.1).
    app.state.dev_origins = list(dev_origins or [])
    app.state.remote_catalog = remote_catalog

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
    # This router already owns the full /api/remote/servers prefix (§5.2.1).
    # I9: its catalog GET reaches the owner's strict-v2 read facade; the holder's
    # snapshot/legacy/exact-get facades stay backend-only (no new routes).
    remote_router = create_remote_router(
        metadata=remote_catalog, profiles=remote_catalog,
        verification=remote_catalog, catalog=remote_catalog, mutations=remote_catalog,
        creation=remote_catalog,
    )
    app.include_router(remote_router)
    app.include_router(ws_router)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # §5.2.1: even rejected settings can contain credentials. Do not serialize
        # errors() here: input, ctx, custom messages and dynamic locations may all
        # contain request values. Other APIs retain FastAPI's existing diagnostics.
        if request.scope.get("endpoint") in (
            update_settings, update_application_settings, update_project_settings,
        ):
            return JSONResponse(status_code=422, content={"detail": [{
                "type": "value_error", "loc": ["body"], "msg": "Invalid settings request.",
            }]})
        return await request_validation_exception_handler(request, exc)

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

    _mount_frontend(app, remote_routes=remote_router.routes)
    return app


def _mount_frontend(app: FastAPI, *, remote_routes: Sequence[BaseRoute] = ()) -> None:
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

    class SpaFallbackRoute(APIRoute):
        def matches(self, scope):
            match, child_scope = super().matches(scope)
            # A GET catch-all must not swallow Remote's method refusals (§5.2.1).
            # Leave routing of every other API/project path exactly as before.
            if match == Match.FULL and any(
                route.matches(scope)[0] == Match.PARTIAL for route in remote_routes
            ):
                return Match.NONE, {}
            return match, child_scope

    async def spa_fallback(full_path: str) -> FileResponse:
        """Serve real files, otherwise hand the route to the SPA router."""
        if full_path.split("/", 1)[0] in ("api", "ws"):
            # Not the SPA's to answer: an endpoint this backend does not have is a 404 the
            # client can read, not a page of HTML with status 200 (§2.5).
            raise HTTPException(status_code=404, detail=f"Not found: /{full_path}")
        candidate = (directory / full_path).resolve()
        if directory in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_file, headers=no_cache)

    app.router.add_api_route(
        "/{full_path:path}", spa_fallback, methods=["GET"], include_in_schema=False,
        route_class_override=SpaFallbackRoute,
    )

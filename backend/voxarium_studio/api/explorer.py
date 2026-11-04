"""Explorer endpoints (§6.2, §11.1).

The file operations here are deliberately thin: every rule about what may be created,
renamed, moved or deleted lives in ``ExplorerService``, so the MCP tools and any future
caller are bound by the same checks as a right-click in the browser. What this module
owns is the wire — the request bodies, and which exception becomes which status.

None of them publishes ``explorer.file_changed``. The watcher is the single producer of
that event, which is what makes a UI that is correct for an external edit correct for
its own click too.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core.reveal import RevealError, reveal
from ..core.security import UNTRUSTED_FILE_HEADERS
from ..explorer.service import OperationRefused, PartialOperation, PathOutsideProject
from .deps import Ctx
from .plugins import previewer_json

router = APIRouter(prefix="/explorer", tags=["explorer"])


class CreateEntryRequest(BaseModel):
    parent: str = ""
    """Directory the new entry goes in. The empty string is the project root."""

    name: str
    kind: Literal["file", "directory"] = "file"


class RenameRequest(BaseModel):
    path: str
    name: str
    """A new basename, not a path — a separator in it is refused with 400."""


class TransferRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)
    destination: str = ""
    """Destination *directory*, project-relative."""

    overwrite: bool = False


class DeleteRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class RevealRequest(BaseModel):
    path: str
    """A single entry — one file manager window is the point, so this is not a list."""


def _failure(exc: Exception, done: list[str] | None = None) -> HTTPException:
    """Map a service exception onto a status, carrying what already landed.

    ``detail`` stays a plain string in the ordinary case, because that is what the
    client renders. It becomes ``{"message", "paths"}`` only when part of a multi-path
    operation succeeded before the rest failed — there the extra data is the point, and
    a client that only prints the detail still shows the message inside it.
    """
    if isinstance(exc, PathOutsideProject):
        status = 403
    elif isinstance(exc, FileNotFoundError):
        status = 404
    elif isinstance(exc, (FileExistsError, OperationRefused)):
        status = 409
    elif isinstance(exc, ValueError):
        status = 400
    elif isinstance(exc, OSError):
        status = 500
    else:  # pragma: no cover - the service raises nothing else
        status = 500
    message = str(exc) or exc.__class__.__name__
    detail: Any = {"message": message, "paths": done} if done else message
    return HTTPException(status_code=status, detail=detail)


# Everything the service raises on a refusal. Catching the union in one place is what
# keeps each route to its single call.
ServiceError = (PathOutsideProject, OperationRefused, OSError, ValueError)


@router.get("/tree")
async def get_tree(
    ctx: Ctx,
    path: str = Query(default="", description="Directory relative to the project root"),
    depth: int = Query(default=1, ge=1, le=4),
) -> dict[str, Any]:
    try:
        return ctx.explorer.tree(path, depth=depth)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/file")
async def get_file(ctx: Ctx, path: str = Query(...)) -> dict[str, Any]:
    try:
        payload = ctx.explorer.preview(path).to_json()
        # The service itself knows nothing about plugins; matching a claimed file type
        # happens here so the client gets the viewer in the same round trip (§6.2).
        previewer = (
            None
            if payload["kind"] == "directory"
            else ctx.registry.previewer_for(PurePosixPath(payload["path"]).suffix)
        )
        payload["previewer"] = previewer_json(previewer, ctx.registry) if previewer else None
        if previewer is not None:
            # The service's "no viewer for this type" note is stale once one exists.
            payload["message"] = None
        return payload
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Not found: {path}") from exc


@router.get("/raw")
async def get_raw(ctx: Ctx, path: str = Query(...)) -> FileResponse:
    """Stream a file as-is — used for large images and for downloading artifacts."""
    try:
        resolved = ctx.explorer.resolve(path)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {path}")
    # Project files change under the client — that is the whole point of the watcher —
    # and without this the browser's heuristic caching serves a plugin previewer the
    # bytes from before a pipeline node regenerated the artifact.
    return FileResponse(
        resolved, filename=resolved.name,
        headers={**UNTRUSTED_FILE_HEADERS, "Cache-Control": "no-cache"},
    )


@router.post("/create")
async def create_entry(payload: CreateEntryRequest, ctx: Ctx) -> dict[str, list[str]]:
    try:
        return {"paths": ctx.explorer.create(payload.parent, payload.name, payload.kind)}
    except ServiceError as exc:
        raise _failure(exc) from exc


@router.post("/rename")
async def rename_entry(payload: RenameRequest, ctx: Ctx) -> dict[str, list[str]]:
    try:
        return {"paths": ctx.explorer.rename(payload.path, payload.name)}
    except ServiceError as exc:
        raise _failure(exc) from exc


@router.post("/move")
async def move_entries(payload: TransferRequest, ctx: Ctx) -> dict[str, list[str]]:
    try:
        moved = ctx.explorer.move(
            payload.paths, payload.destination, overwrite=payload.overwrite
        )
    except PartialOperation as exc:
        raise _failure(exc.cause, exc.done) from exc
    except ServiceError as exc:
        raise _failure(exc) from exc
    return {"paths": moved}


@router.post("/copy")
async def copy_entries(payload: TransferRequest, ctx: Ctx) -> dict[str, list[str]]:
    try:
        copied = ctx.explorer.copy(
            payload.paths, payload.destination, overwrite=payload.overwrite
        )
    except PartialOperation as exc:
        raise _failure(exc.cause, exc.done) from exc
    except ServiceError as exc:
        raise _failure(exc) from exc
    return {"paths": copied}


@router.post("/delete")
async def delete_entries(payload: DeleteRequest, ctx: Ctx) -> dict[str, list[str]]:
    try:
        removed = ctx.explorer.delete(payload.paths)
    except PartialOperation as exc:
        raise _failure(exc.cause, exc.done) from exc
    except ServiceError as exc:
        raise _failure(exc) from exc
    return {"paths": removed}


@router.post("/reveal")
async def reveal_entry(payload: RevealRequest, ctx: Ctx) -> dict[str, object]:
    """Open the entry's location in the machine's own file manager (§6.2).

    Loopback-only, for the same reason `POST /api/fs/pick` is: the window opens on the
    desktop of whoever runs the server, so on a networked Studio it would appear on a
    screen the caller cannot see. That is a 409 with something to read, not a silent
    no-op — the button exists in the menu either way, and the refusal is what explains it.
    """
    if not ctx.auth.is_loopback:
        raise HTTPException(
            status_code=409,
            detail=(
                "This Studio is not running on your machine, so it cannot open a file "
                "manager window for you — it would appear on the server's desktop."
            ),
        )

    try:
        target = ctx.explorer.resolve(payload.path)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {payload.path}")

    try:
        result = await reveal(target)
    except RevealError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return result.to_json()

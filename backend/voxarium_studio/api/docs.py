"""Project documentation — the team's other medium next to the task board (§10.4).

Route order matters: the literal paths are declared before ``/{path:path}``, which would
otherwise swallow them. ``move`` and the folder operations are POSTs for the same reason —
the catch-all only claims GET, PUT and DELETE.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..core.security import UNTRUSTED_FILE_HEADERS
from ..explorer.service import PathOutsideProject
from ..teams.docs import MAX_ASSET_BYTES, DocumentChangedOnDisk, FolderInUse, FolderStyleUnreadable
from .deps import Ctx

router = APIRouter(prefix="/docs", tags=["docs"])


class DocWrite(BaseModel):
    content: str
    author: str | None = None
    """Member id when a person writes as themselves; null means the user."""
    expected_updated_at: str | None = None
    """The ``updated_at`` the client last read. Sent, the write refuses to clobber a
    newer file and answers 409; omitted, it writes unconditionally (what agents do)."""
    exclusive: bool = False
    """Refuse with 409 when anything is already at the path — what creating a document sends,
    so a name that is taken is reported instead of written over."""


class DocMove(BaseModel):
    source: str
    target: str
    author: str | None = None


class FolderStyleUpdate(BaseModel):
    """A patch to a folder's look: a key sent as null goes back to the default, one left out
    is left alone (block-editor spec §5.2)."""

    path: str
    color: str | None = None
    icon: str | None = None
    author: str | None = None


class FolderRenameRequest(BaseModel):
    path: str
    """The folder, relative to the docs root — ``features`` or ``features/combat``."""
    name: str
    """Its new name: a name, not a path, so the folder stays where it is."""
    author: str | None = None


class FolderDeleteRequest(BaseModel):
    path: str
    """The folder, relative to the docs root. It goes with everything in it."""
    author: str | None = None


@router.get("")
async def list_docs(ctx: Ctx) -> list[dict[str, Any]]:
    return [entry.to_json() for entry in ctx.docs.list_docs()]


@router.get("/search")
async def search_docs(
    ctx: Ctx,
    q: str = Query(default="", description="Case-insensitive substring"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    return ctx.docs.search(q, limit=limit)


@router.get("/graph")
async def docs_graph(ctx: Ctx) -> dict[str, Any]:
    """The whole wiki-link graph in one response — nodes, edges and degrees (§10.4).

    Above ``/{path:path}`` like ``/search``, or the catch-all would claim the word ``graph``
    and a document could never be read at all. It is built from the same index a read uses,
    so the map costs no more than opening a document.
    """
    return ctx.docs.graph()


@router.get("/folders")
async def list_doc_folders(ctx: Ctx) -> list[dict[str, Any]]:
    """Every folder of the tree that has a colour or an icon (block-editor spec §5.2).

    Above ``/{path:path}`` for the reason ``/search`` is. The tree itself still comes from the
    document listing — a folder is a prefix of some document's path — so this answers only how
    the folders that have said something look.
    """
    return ctx.docs.folders()


@router.get("/asset")
async def read_doc_asset(
    request: Request,
    ctx: Ctx,
    path: str = Query(..., description="The document the reference was found in"),
    src: str = Query(..., description="The reference, exactly as it stands in the Markdown"),
) -> Response:
    """The bytes of a picture a document points at (§10.4).

    Above ``/{path:path}`` for the same reason ``/search`` and ``/graph`` are: the catch-all
    would claim the word ``asset``. Both halves of the reference travel as query parameters
    rather than in the path — an exported document names its files things like
    ``ChatGPT_Image(1).png``, and a path segment is the wrong place to argue about that.

    Answered conditionally. These are photographs and screenshots, several megabytes each,
    and a document holding a dozen of them is re-rendered every time the editor remounts;
    revalidating costs one round trip on the loopback instead of the whole file. The tag is
    the file's own size and mtime, so a picture replaced on disk still comes back changed.
    """
    try:
        target = ctx.docs.asset(path, src)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    stat = target.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {**UNTRUSTED_FILE_HEADERS, "ETag": etag, "Cache-Control": "no-cache"}
    if _matches_etag(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    # `inline`, unlike the Explorer's `/raw`, which is also a download link: this URL is only
    # ever an `<img>` source. The name still rides along, so "save image as" offers the file's
    # own name rather than `asset`.
    return FileResponse(
        target, filename=target.name, content_disposition_type="inline", headers=headers
    )


@router.post("/asset", status_code=201)
async def add_doc_asset(
    ctx: Ctx,
    path: str = Form(..., description="The document the picture is being added to"),
    file: UploadFile = File(..., description="The picture itself"),
) -> dict[str, Any]:
    """Put a dropped picture beside a document and answer with the reference to write.

    Multipart rather than JSON: the file is already bytes on the client and base64 would
    inflate a 20 MiB screenshot by a third for no gain. The *editor* decides what to do with
    the answer — this endpoint never touches the Markdown, so a failed insert leaves a spare
    file rather than a broken document.
    """
    try:
        data = await file.read(MAX_ASSET_BYTES + 1)
        return ctx.docs.add_asset(path, file.filename or "", data)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await file.close()


def _matches_etag(header: str | None, etag: str) -> bool:
    """Whether the browser already holds this exact version.

    ``*`` matches anything, a list is any of its members, and a ``W/`` prefix is dropped —
    we only ever issue strong tags, and a cache is entitled to weaken one.
    """
    if not header:
        return False
    candidates = [value.strip() for value in header.split(",")]
    return "*" in candidates or any(
        value.removeprefix("W/") == etag for value in candidates if value
    )


@router.post("/move")
async def move_doc(payload: DocMove, ctx: Ctx) -> dict[str, Any]:
    try:
        return await ctx.docs.move(payload.source, payload.target, author=payload.author)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/folders/style")
async def style_doc_folder(payload: FolderStyleUpdate, ctx: Ctx) -> dict[str, Any]:
    """Set or clear a folder's colour and icon, in its ``vxs-folder.json``.

    409 when that file exists and does not parse: it is refused rather than overwritten,
    because whatever is in it was put there by somebody.
    """
    sent = payload.model_fields_set
    changes = {key: getattr(payload, key) for key in ("color", "icon") if key in sent}
    try:
        return await ctx.docs.set_folder_style(payload.path, changes, author=payload.author)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FolderStyleUnreadable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/folders/rename")
async def rename_doc_folder(payload: FolderRenameRequest, ctx: Ctx) -> StreamingResponse:
    """Rename a folder, then mend the links into it — answered as a stream of progress.

    Everything that can refuse the rename is answered as an ordinary status code, because it
    happens before the response starts: 404 for a folder that is not there, 409 for a name
    already taken or a folder the system will not let go of, 400 for a name the tree cannot
    hold. Once the folder is renamed the answer is ``200`` and newline-delimited JSON, one frame
    per line (``renamed``, a ``document`` per file mended, ``done`` or ``error``) — so the
    dialog shows each document as it is written rather than a spinner and then a list.

    A stream rather than events on the socket: the progress belongs to the one request that
    asked, and the bus would broadcast it to every client and keep it in the replay buffer
    other clients resume from. What other clients do need — that a folder moved and which
    documents changed — is published there by the store.
    """
    try:
        operation = await ctx.docs.rename_folder(payload.path, payload.name, author=payload.author)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (FileExistsError, FolderInUse) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    async def lines(frames: AsyncIterator[dict[str, object]]) -> AsyncIterator[str]:
        async for frame in frames:
            yield json.dumps(frame, ensure_ascii=False) + "\n"

    return StreamingResponse(
        lines(operation.frames()),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/folders/delete")
async def delete_doc_folder(payload: FolderDeleteRequest, ctx: Ctx) -> dict[str, Any]:
    """Delete a folder of the tree with everything in it, and name the documents that went.

    409 when the system will not let go of the folder — refused before anything is removed, see
    ``DocStore.delete_folder``. 500 for a removal that stopped part-way, whose message says where
    what is left went.
    """
    try:
        return await ctx.docs.delete_folder(payload.path, author=payload.author)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FolderInUse as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{path:path}")
async def read_doc(path: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return ctx.docs.read(path)
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/{path:path}")
async def write_doc(path: str, payload: DocWrite, ctx: Ctx) -> dict[str, Any]:
    try:
        return await ctx.docs.write(
            path,
            payload.content,
            author=payload.author,
            expected_updated_at=payload.expected_updated_at,
            exclusive=payload.exclusive,
        )
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (DocumentChangedOnDisk, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/{path:path}", status_code=200)
async def delete_doc(path: str, ctx: Ctx) -> dict[str, Any]:
    try:
        return {"path": await ctx.docs.delete(path), "deleted": True}
    except PathOutsideProject as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

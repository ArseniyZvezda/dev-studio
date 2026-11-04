"""Project-local uploads for task briefs and chat messages (§10.4, §10.9)."""
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..core.security import UNTRUSTED_FILE_HEADERS
from ..teams.attachments import MAX_ATTACHMENT_BYTES
from .deps import Ctx

router = APIRouter(prefix="/attachments", tags=["attachments"])


@router.post("", status_code=201)
async def upload(ctx: Ctx, file: UploadFile = File(...)):
    try:
        data = await file.read(MAX_ATTACHMENT_BYTES + 1)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(413, "File exceeds the 20 MiB limit")
        folder = "upload_" + uuid4().hex
        record = ctx.teams.attachments.add(folder, file.filename or "", data)
        return {**record, "reference": f"{folder}/{record['name']}"}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        await file.close()


@router.get("/{folder}/{name}")
async def download(folder: str, name: str, ctx: Ctx):
    try:
        record = ctx.teams.attachments.resolve_uploads([f"{folder}/{name}"])[0]
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(
        ctx.teams.attachments.absolute(record),
        filename=record["name"],
        media_type=record["media_type"] or "application/octet-stream",
        content_disposition_type="inline" if record["image"] else "attachment",
        headers={**UNTRUSTED_FILE_HEADERS, "Cache-Control": "no-cache"},
    )

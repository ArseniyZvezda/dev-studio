"""Files a reviewer attaches to a review comment (§10.4).

A screenshot of the wrong pixel, a reference picture, a log — what "request changes" often
needs to say cannot be said in words. The files land under ``voxarium_studio/attachments/<task>/``,
the review entry in the transcript records them, and the session that resumes is handed the
pictures as image blocks and every file by its absolute path, with a Read rule on the
folder so an isolated worktree can open them.

Stored and never trusted: only the last path component of a dropped name survives, illegal
characters become dashes, and an existing file is never overwritten — ``shot.png`` becomes
``shot-2.png``, visible in the folder the way a silent replacement is not. The same rules
``DocStore.add_asset`` applies to a picture dropped into a document, for the same reasons.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePath
from typing import Any

from ..core.paths import ProjectPaths
from .skill import rule_path

#: One attachment may not exceed this. Pictures are what arrives here, and a 4K screenshot is
#: a few megabytes; the ceiling is the Explorer's preview ceiling, for the same reason.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

#: What reaches the model as an *image block* rather than as a path. The four the API
#: accepts; anything else is a file the agent can still ``Read`` by its path.
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_name(filename: str) -> str:
    """The name a file is stored under: the last component, cleaned, never empty."""
    tail = PurePath(filename.replace("\\", "/")).name
    cleaned = _ILLEGAL.sub("-", tail).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("The attachment needs a file name")
    return cleaned


def media_type_of(name: str) -> str | None:
    """The image media type a name implies, or ``None`` for anything that is not a picture."""
    return IMAGE_MEDIA_TYPES.get(PurePath(name).suffix.lower())


class AttachmentStore:
    """Review attachments on disk, one folder per task."""

    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths

    def folder(self, task_id: str) -> Path:
        if not _TASK_ID.match(task_id):
            raise ValueError(f"Not a task id: {task_id!r}")
        return self.paths.attachments / task_id

    def add(self, task_id: str, filename: str, data: bytes) -> dict[str, Any]:
        """Store ``data`` under the task and answer with the record the review entry keeps."""
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(
                f"The attachment is {len(data)} bytes; the limit is {MAX_ATTACHMENT_BYTES}"
            )
        name = safe_name(filename)
        folder = self.folder(task_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        stem, suffix = PurePath(name).stem, PurePath(name).suffix
        counter = 2
        while target.exists():
            target = folder / f"{stem}-{counter}{suffix}"
            counter += 1
        target.write_bytes(data)
        return self.describe(task_id, target.name)

    def path_for(self, task_id: str, name: str) -> Path:
        """The file a stored name refers to — refusing anything that is not a bare name."""
        if name != safe_name(name) or name != PurePath(name).name:
            raise ValueError(f"Not an attachment name: {name!r}")
        target = self.folder(task_id) / name
        if not target.is_file():
            raise FileNotFoundError(f"No attachment named {name!r} on this task")
        return target

    def describe(self, task_id: str, name: str) -> dict[str, Any]:
        """The record a review entry carries: name, project-relative path, size, kind."""
        target = self.path_for(task_id, name)
        media_type = media_type_of(name)
        return {
            "name": target.name,
            "path": target.relative_to(self.paths.root).as_posix(),
            "size": target.stat().st_size,
            "media_type": media_type,
            "image": media_type is not None,
        }

    def remove(self, task_id: str, name: str) -> None:
        self.path_for(task_id, name).unlink()

    def resolve(self, task_id: str, names: list[str]) -> list[dict[str, Any]]:
        """The records for the names a review names, in that order; a missing one raises."""
        return [self.describe(task_id, name) for name in names]

    def resolve_uploads(self, references: list[str]) -> list[dict[str, Any]]:
        """Resolve opaque upload references, never client-supplied paths (§10.4, §10.9)."""
        records = []
        for reference in dict.fromkeys(references):
            folder, separator, name = reference.partition("/")
            if not separator or not re.fullmatch(r"upload_[0-9a-f]{32}", folder):
                raise ValueError("Invalid attachment reference")
            try:
                record = self.describe(folder, name)
            except FileNotFoundError as exc:
                raise ValueError("Attachment no longer exists") from exc
            record["reference"] = reference
            records.append(record)
        return records

    # ------------------------------------------------------------- what the session gets

    def absolute(self, record: dict[str, Any]) -> Path:
        return self.paths.root / str(record["path"])

    def images(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pictures among ``records`` as the backend wants them: absolute path and type."""
        return [
            {"path": str(self.absolute(record)), "media_type": record["media_type"]}
            for record in records
            if record.get("image") and record.get("media_type")
        ]

    def trailer(self, records: list[dict[str, Any]]) -> str:
        """The lines appended to a review comment naming every file by absolute path.

        Pictures are named too, although they also travel as image blocks: the path is what
        lets the agent open one again on a later turn, after the block has scrolled out of
        reach, and what a backend with no image channel is left with.
        """
        if not records:
            return ""
        lines = ["Attached files (open with Read):"]
        for record in records:
            kind = " (image)" if record.get("image") else ""
            lines.append(f"- {self.absolute(record)}{kind}")
        return "\n".join(lines)

    def allowed_tools(self) -> list[str]:
        """A Read rule on the attachments folder, so an isolated checkout can open them."""
        where = rule_path(self.paths.attachments)
        return [f"Read({where}/**)"] if where is not None else []


def with_trailer(comment: str, trailer: str) -> str:
    return f"{comment.rstrip()}\n\n{trailer}" if trailer else comment

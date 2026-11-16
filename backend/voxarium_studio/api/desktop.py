"""Machine-local desktop topology, not project state (dev spec §5.3.6).

Only this backend owns the path. Main submits committed topology snapshots; this
module neither opens projects nor restores windows. Unknown/corrupt files are
errors, never an empty session or permission to overwrite a newer format.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from ..core.paths import user_config_dir
from ..core.project_scope import canonical_root

router = APIRouter(prefix="/desktop", tags=["desktop"])
log = logging.getLogger(__name__)
MAX_BYTES = 1024 * 1024
MAX_TABS = 256
_session_lock = threading.RLock()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _plain(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError("Control characters are not allowed")
    return value


def _route_path(value: str) -> str:
    _plain(value)
    decoded = unquote(value, errors="strict")
    _plain(decoded)
    if any(c in decoded for c in "\\?#%") or ":" in decoded:
        raise ValueError("Expected a project-relative path, not a URL")
    if "//" in decoded or any(p in {".", ".."} for p in decoded.split("/")):
        raise ValueError("Path traversal is not allowed")
    if not decoded.startswith("/") or decoded.split("/")[1] in {"p", "api", "ws"}:
        raise ValueError("Expected a project-relative route without runtime identity")
    return value


def _file_path(value: str) -> str:
    # Explorer's selector is a literal filename, not a URL (dev spec §5.3.6).
    # In particular, neither '#' nor percent escapes may be decoded or lost.
    _plain(value)
    if any(c in value for c in "\\?:"):
        raise ValueError("Expected a project-relative path, not a URL")
    if "//" in value or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("Path traversal is not allowed")
    if value.startswith("/") or PureWindowsPath(value).is_absolute():
        raise ValueError("Expected a project-relative file path")
    return value


Selector = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:-]+$")]


class PageQuery(_StrictModel):
    # Explicit selectors retain consumed deep links; no arbitrary query payload,
    # token, cursor, draft, Yjs state or File object can enter the manifest.
    path: Annotated[str, Field(max_length=4096)] | None = None
    task: Selector | None = None
    chat: Selector | None = None
    assign: Selector | None = None

    @field_validator("path", "task", "chat", "assign", mode="before")
    @classmethod
    def non_null(cls, value):
        if value is None:
            raise ValueError("Omit absent selectors")
        return value

    @field_validator("path")
    @classmethod
    def file_path(cls, value: str) -> str:
        return _file_path(value)


class Page(_StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=2048)]
    query: PageQuery = Field(default_factory=PageQuery)

    @field_validator("path")
    @classmethod
    def route_path(cls, value: str) -> str:
        return _route_path(value)


class Geometry(_StrictModel):
    # Monitor availability is checked by the native restore caller, not persisted
    # as a display id. Negative coordinates are valid on multi-monitor desktops.
    x: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
    y: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
    width: Annotated[int, Field(ge=100, le=32768)]
    height: Annotated[int, Field(ge=100, le=32768)]
    maximized: bool = False


class Tab(_StrictModel):
    root: Annotated[str, Field(min_length=1, max_length=32768)]
    page: Page

    @field_validator("root")
    @classmethod
    def project_root(cls, value: str) -> str:
        _plain(value)
        if not Path(value).is_absolute():
            raise ValueError("Project root must be absolute")
        try:
            # No exists/init/create check here: an unavailable locator must survive
            # until the future restore layer can report that entry's error.
            return str(canonical_root(value))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("Invalid project root") from exc


class Window(_StrictModel):
    tabs: Annotated[list[Tab], Field(max_length=MAX_TABS)]
    active_tab: int | None
    geometry: Geometry | None = None

    @field_validator("geometry", mode="before")
    @classmethod
    def non_null_geometry(cls, value):
        if value is None:
            raise ValueError("Omit absent geometry")
        return value

    @model_validator(mode="after")
    def active_index(self):
        if not _valid_index(self.active_tab, len(self.tabs)):
            raise ValueError("Invalid active tab index")
        return self


def _valid_index(value: int | None, length: int) -> bool:
    return value is None if not length else value is not None and 0 <= value < length


class Manifest(_StrictModel):
    version: Literal[1]
    windows: Annotated[list[Window], Field(max_length=32)]
    active_window: int | None

    @model_validator(mode="after")
    def topology(self):
        if not _valid_index(self.active_window, len(self.windows)):
            raise ValueError("Invalid active window index")
        roots = [tab.root for window in self.windows for tab in window.tabs]
        if len(roots) > MAX_TABS or len(roots) != len(set(roots)):
            raise ValueError("Project tabs must be unique across all windows")
        return self


class SessionError(Exception):
    def __init__(self, code: str, status: int):
        self.code, self.status = code, status
        super().__init__(code)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("Non-finite JSON number")


def _validate(raw: bytes, *, stored: bool) -> dict:
    invalid = SessionError("desktop_session_corrupt" if stored else "desktop_session_invalid", 409 if stored else 422)
    if len(raw) > MAX_BYTES:
        raise invalid
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("Missing version")
        if value["version"] != 1:
            raise SessionError("desktop_session_unsupported_version", 409)
        manifest = Manifest.model_validate(value)
    except (ValueError, ValidationError, RecursionError) as exc:
        raise invalid from exc
    # Only optional fields are omitted; active indices must retain explicit null.
    result = manifest.model_dump()
    for window in result["windows"]:
        if window["geometry"] is None:
            del window["geometry"]
        for tab in window["tabs"]:
            tab["page"]["query"] = {k: v for k, v in tab["page"]["query"].items() if v is not None}
    return result


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise SessionError("desktop_session_missing", 404) from exc
    except OSError as exc:
        raise SessionError("desktop_session_read_failed", 500) from exc
    return _validate(raw, stored=True)


def read_session() -> dict:
    with _session_lock:
        return _read(user_config_dir() / "desktop-session.json")


def write_session(raw: bytes) -> dict:
    manifest = _validate(raw, stored=False)
    payload = json.dumps(manifest, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise SessionError("desktop_session_invalid", 422)
    with _session_lock:
        path = user_config_dir() / "desktop-session.json"
        # Do not silently destroy unknown/newer or damaged state. Recovery is an
        # explicit user operation, not an implicit save or a second snapshot.
        try:
            _read(path)
        except SessionError as exc:
            if exc.code != "desktop_session_missing":
                raise
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=".desktop-session-", suffix=".tmp", dir=path.parent, delete=False) as target:
                temporary = Path(target.name)
                target.write(payload)
                target.flush()
                os.fsync(target.fileno())
            # Same-directory replace publishes only a flushed, complete snapshot.
            os.replace(temporary, path)
        except OSError as exc:
            log.warning("Could not save desktop session", exc_info=True)
            raise SessionError("desktop_session_write_failed", 500) from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove desktop session temporary file", exc_info=True)
    return manifest


def _http_error(exc: SessionError) -> HTTPException:
    return HTTPException(exc.status, detail={"code": exc.code}, headers={"Cache-Control": "no-store"})


# Included only in the authenticated global API, never /projects/{id}/desktop.
# Existing require_auth applies token, Host and Origin checks (dev spec §2.5).
@router.get("/session")
def get_session(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return read_session()
    except SessionError as exc:
        raise _http_error(exc) from exc


@router.put("/session")
async def put_session(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_BYTES:
            raise _http_error(SessionError("desktop_session_too_large", 413))
        raw.extend(chunk)
    try:
        return await run_in_threadpool(write_session, bytes(raw))
    except SessionError as exc:
        raise _http_error(exc) from exc

"""Unwired read-only S3 companion to :mod:`catalog`.

remote-identity/studio-design §6/S3; server-design §3.3–3.4. Only an injected
transport is available. No routes, real HTTP, local storage, Select/Enable,
preview fingerprint or sync lifecycle are implemented here. A complete snapshot
is an observation, not permission to overwrite or proof of a future server state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .catalog import (
    CatalogClient, CatalogError, CatalogProject, CatalogResponse, CatalogTransport,
    Verification, _error, _id, _object, _project, _version,
)
from .protocol import content_hash


# Private bounds for the opt-in contract, not changes to live v1 constants.
_PAGE_BYTES = 4 * 1024 * 1024
_DOC_BYTES = 1_000_000


@dataclass(frozen=True)
class SnapshotDocument:
    path: str
    content: str | None = field(repr=False)
    hash: str | None
    rev: int
    deleted: bool
    updated_by: str | None
    updated_at: str


@dataclass(frozen=True)
class ProjectSnapshot:
    id: str
    storage_epoch: str
    seq: int
    documents: tuple[SnapshotDocument, ...] = field(repr=False)

    @property
    def space_instance_id(self) -> str:
        """Exact local alias of wire storage_epoch, never derived from id."""
        return self.storage_epoch


@dataclass(frozen=True)
class LegacyResolution:
    legacy_space_name: str
    project: CatalogProject


def _epoch(value: object, code: str = "invalid_response") -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise CatalogError(code)
    return value


def _utf8_size(value: str) -> int:
    size = None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        pass
    if size is None:
        raise CatalogError("invalid_response")
    return size


def _json_size(body: object) -> int:
    """Bound the decoded fake response's compact UTF-8 representation.

    A future real transport must ALSO bound raw bytes before JSON decoding;
    this injected decoded-JSON boundary cannot measure HTTP whitespace/encoding.
    Never retain serialization exceptions (they can contain document/key input).
    """
    size = 0
    invalid = False
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                      separators=(",", ":")).iterencode(body):
            size += len(chunk.encode("utf-8"))
            if size > _PAGE_BYTES:
                break
    except (TypeError, ValueError, UnicodeError, RecursionError):
        invalid = True
    if invalid:
        raise CatalogError("invalid_response")
    if size > _PAGE_BYTES:
        raise CatalogError("payload_too_large")
    return size


def _path(value: object) -> str:
    # Restated from the document wire owner, remote/protocol.py:check_path.
    # A stored path is already canonical: do not trim/repair cursor or document.
    if (not isinstance(value, str) or not 1 <= len(value) <= 512
            or value != value.strip() or "\\" in value or value.startswith("/")
            or any(ord(c) < 32 for c in value)
            or any(p in {"", ".", ".."} for p in value.split("/"))
            or PurePosixPath(value).suffix.lower() not in {".md", ".markdown", ".txt"}):
        raise CatalogError("invalid_response")
    _utf8_size(value)
    return value


def _document(value: object, seq: int) -> SnapshotDocument:
    raw = _object(value, {"path", "content", "hash", "rev", "deleted", "updated_by", "updated_at"})
    _path(raw["path"])
    if (type(raw["rev"]) is not int or not 1 <= raw["rev"] <= seq
            or type(raw["deleted"]) is not bool
            or (raw["updated_by"] is not None
                and (not isinstance(raw["updated_by"], str) or not raw["updated_by"]))
            or not isinstance(raw["updated_at"], str) or not raw["updated_at"]):
        raise CatalogError("invalid_response")
    if raw["deleted"]:
        if raw["content"] is not None or raw["hash"] is not None:
            raise CatalogError("invalid_response")
    else:
        if (not isinstance(raw["content"], str) or not isinstance(raw["hash"], str)
                or re.fullmatch(r"[0-9a-f]{64}", raw["hash"]) is None):
            raise CatalogError("invalid_response")
        if _utf8_size(raw["content"]) > _DOC_BYTES:
            raise CatalogError("payload_too_large")
        if content_hash(raw["content"]) != raw["hash"]:
            raise CatalogError("invalid_response")
    return SnapshotDocument(**raw)


def _read_error(response: CatalogResponse) -> str:
    # The accepted catalog intentionally lacks the new legacy-only 404 code.
    # Recognise only the exact envelope/code; never pass message/request_id on.
    body = response.body
    if response.status == 404 and isinstance(body, dict) and body.keys() == {"error"}:
        raw = body["error"]
        if (isinstance(raw, dict) and raw.keys() == {"code", "message", "request_id"}
                and raw["code"] == "legacy_space_not_found"
                and isinstance(raw["message"], str) and isinstance(raw["request_id"], str)):
            return "legacy_space_not_found"
    return _error(response)


class CatalogReads:
    """GET-only facade reusing catalog verification and redacted transport.

    Composition deliberately does not expose catalog Create. Verification is
    ephemeral; concurrent reverification invalidates in-flight observations.
    Failures discard function-local accumulation only. No automatic retry, draft,
    base, queue, configuration or previously returned snapshot is accessible.
    """

    def __init__(self, transport: CatalogTransport, *, url: str, token: str):
        self._catalog = CatalogClient(transport, url=url, token=token)

    async def verify(self) -> Verification:
        return await self._catalog.verify()

    def _fence(self, capability: str, generation: int | None = None) -> int:
        verification = self._catalog._verified()
        if capability not in verification.capabilities:
            raise CatalogError("capabilities_missing")
        current = self._catalog._verification_generation
        if generation is not None and current != generation:
            raise CatalogError("verification_stale")
        return current

    async def _get(self, path: str, params: dict[str, str | int], *,
                   capability: str, generation: int) -> object:
        self._fence(capability, generation)
        response = await self._catalog._request("GET", path, params=params)
        if response.status != 200:
            raise CatalogError(_read_error(response))
        self._fence(capability, generation)
        return response.body

    async def snapshot(
        self, id: str, *, expected_epoch: str | None = None, limit: int = 20,
        max_pages: int = 100, max_bytes: int = 64 * 1024 * 1024,
    ) -> ProjectSnapshot:
        """Return all pages from one exact id/epoch/seq, or raise with no result.

        server-design §3.3: every continuation binds the first page's seq/epoch.
        409 or any malformed/failed page aborts, without trying a new snapshot
        behind the caller's back. Bounds include tombstones and JSON overhead.
        """
        capability = "project_snapshot"
        generation = self._fence(capability)
        project_id = _id(id)
        if expected_epoch is not None:
            _epoch(expected_epoch, "invalid_request")
        if (type(limit) is not int or not 1 <= limit <= 100
                or type(max_pages) is not int or max_pages < 1
                or type(max_bytes) is not int or max_bytes < 1):
            raise CatalogError("invalid_request")
        params: dict[str, str | int] = {"limit": limit}
        if expected_epoch is not None:
            params["expected_epoch"] = expected_epoch
        documents: list[SnapshotDocument] = []
        epoch = expected_epoch
        seq = None
        after = None
        total = 0
        for _ in range(max_pages):
            body = await self._get(f"/api/v2/projects/{project_id}/snapshot", params,
                                   capability=capability, generation=generation)
            total += _json_size(body)
            if total > max_bytes:
                raise CatalogError("snapshot_size_limit")
            raw = _object(body, {"protocol", "snapshot", "documents", "next_after"})
            _version(raw["protocol"])
            identity = _object(raw["snapshot"], {"id", "storage_epoch", "seq"})
            if _id(identity["id"], "invalid_response") != project_id:
                raise CatalogError("identity_mismatch")
            received_epoch = _epoch(identity["storage_epoch"])
            received_seq = identity["seq"]
            if type(received_seq) is not int or received_seq < 0:
                raise CatalogError("invalid_response")
            if epoch is not None and epoch != received_epoch:
                raise CatalogError("snapshot_changed")
            if seq is not None and seq != received_seq:
                raise CatalogError("snapshot_changed")
            epoch, seq = received_epoch, received_seq
            if not isinstance(raw["documents"], list) or len(raw["documents"]) > limit:
                raise CatalogError("invalid_response")
            page = tuple(_document(d, seq) for d in raw["documents"])
            previous = after
            for document in page:
                if previous is not None and document.path.encode("utf-8") <= previous.encode("utf-8"):
                    raise CatalogError("invalid_response")
                previous = document.path
            cursor = raw["next_after"]
            if cursor is not None:
                _path(cursor)
                if not page or cursor != page[-1].path:
                    raise CatalogError("invalid_response")
            documents.extend(page)
            if cursor is None:
                return ProjectSnapshot(project_id, epoch, seq, tuple(documents))
            after = cursor
            params = {"limit": limit, "after": after, "expected_seq": seq, "expected_epoch": epoch}
        raise CatalogError("pagination_limit")

    async def resolve_legacy(self, name: str) -> LegacyResolution:
        """Exact migration evidence, not an alias, fallback connect or consent."""
        capability = "legacy_resolution"
        generation = self._fence(capability)
        if (not isinstance(name, str) or not 1 <= len(name) <= 512
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in name)):
            raise CatalogError("invalid_request")
        # Do not trim/lower/NFC or run the canonical project-id validator.
        if _utf8_size(name) > 2048:
            raise CatalogError("invalid_request")
        body = await self._get("/api/v2/legacy-spaces/resolve", {"name": name},
                               capability=capability, generation=generation)
        _json_size(body)
        raw = _object(body, {"protocol", "legacy_space_name", "project"})
        _version(raw["protocol"])
        if raw["legacy_space_name"] != name:
            raise CatalogError("identity_mismatch")
        return LegacyResolution(name, _project(raw["project"]))

"""``.mcp.json`` — the project's MCP servers (§10.7).

The file is Claude Code's own project-scoped MCP config, and it lives at the root of the
managed project rather than inside ``.voxarium_studio/``: the `claude` CLI reads it from there, so
a developer's terminal and the Studio see the same servers. That also means this module
is a **guest** in someone else's file. Two rules follow from it:

- Nothing is dropped on a write. Keys the Studio does not model — top-level ones, and
  per-server ones — are carried through, so a field a future CLI version adds survives an
  edit made here.
- A document that does not parse is never rewritten. The API answers with the error and
  the raw text instead, and the page offers to edit that text directly; silently replacing
  a file someone is mid-edit with the Studio's idea of its contents is the one failure
  that costs work.

Which servers an agent actually gets is *not* recorded here — that is
``config.mcp.disabled`` (§10.7), because switching one off for the Studio must not remove
it from the file the terminal reads.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import AppConfig
from .paths import ProjectPaths

log = logging.getLogger(__name__)

SERVERS_KEY = "mcpServers"

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
"""A server's name becomes part of every tool it exposes (``mcp__<name>__<tool>``), so a
name with a space or a slash in it produces tool names no permission rule can match."""

#: Per-server keys this module models. Anything else in an entry is preserved verbatim.
KNOWN_SERVER_KEYS = frozenset({"type", "command", "args", "env", "url", "headers"})


class StdioServer(BaseModel):
    """A server the CLI launches as a subprocess."""

    type: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class RemoteServer(BaseModel):
    """A server reached over HTTP, either streamable (``http``) or legacy SSE."""

    type: Literal["http", "sse"]
    url: str = Field(min_length=1)
    headers: dict[str, str] = Field(default_factory=dict)


McpServer = StdioServer | RemoteServer


class McpFileError(Exception):
    """The file cannot be read, parsed, or safely rewritten."""


def parse_server(raw: Any) -> McpServer:
    """Validate one entry of ``mcpServers``.

    ``type`` is optional in the format and absent from most hand-written stdio entries,
    so it is inferred: an entry carrying a ``url`` is remote, anything else is a
    subprocess. That matches how the CLI reads the same file.
    """
    if not isinstance(raw, dict):
        raise McpFileError("must be an object")
    kind = raw.get("type") or ("http" if "url" in raw else "stdio")
    try:
        if kind == "stdio":
            return StdioServer.model_validate({**raw, "type": "stdio"})
        if kind in ("http", "sse"):
            return RemoteServer.model_validate({**raw, "type": kind})
    except ValidationError as exc:
        raise McpFileError(_first_error(exc)) from exc
    raise McpFileError(f"unknown transport {kind!r} — expected stdio, http or sse")


def _first_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "server"
    return f"{where}: {first['msg']}"


def server_payload(server: McpServer) -> dict[str, Any]:
    """The entry as it is written to disk and handed to the SDK.

    Empty collections are dropped: a hand-written file does not carry ``"env": {}``, and
    a round trip through the Studio should not start adding them.
    """
    payload = server.model_dump(mode="json")
    return {key: value for key, value in payload.items() if value not in ({}, [])}


@dataclass(slots=True)
class McpFile:
    """A parsed ``.mcp.json``, with everything needed to write it back untouched."""

    path: Path
    exists: bool
    text: str = ""
    document: dict[str, Any] = field(default_factory=dict)
    servers: dict[str, McpServer] = field(default_factory=dict)
    invalid: dict[str, str] = field(default_factory=dict)
    """Entries that are present but unusable, by name — shown rather than swallowed."""
    error: str | None = None
    """Set when the *document* is unusable, which makes every write refuse."""

    @property
    def raw_servers(self) -> dict[str, Any]:
        servers = self.document.get(SERVERS_KEY)
        return servers if isinstance(servers, dict) else {}

    def require_writable(self) -> None:
        if self.error is not None:
            raise McpFileError(
                f"{self.path.name} could not be parsed ({self.error}). Fix the file "
                "itself before editing servers, or the edit would overwrite it."
            )


def read_mcp_file(path: Path) -> McpFile:
    """Read and parse the file, reporting problems instead of raising them.

    Every caller here is answering an HTTP request about the state of a file the user
    may be editing by hand, so "it is broken, and this is how" is the useful answer.
    """
    if not path.exists():
        return McpFile(path=path, exists=False, document={SERVERS_KEY: {}})

    try:
        # newline="" keeps CRLF as CRLF. With the default, a Windows-authored file reads
        # back as LF and the first save rewrites every line of it — the same defect the
        # docs layer had, and the raw editor shows this text verbatim.
        with path.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError as exc:
        return McpFile(path=path, exists=True, error=str(exc))

    try:
        document = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        return McpFile(path=path, exists=True, text=text, error=f"line {exc.lineno}: {exc.msg}")

    if not isinstance(document, dict):
        return McpFile(path=path, exists=True, text=text, error="the file is not a JSON object")

    raw_servers = document.get(SERVERS_KEY, {})
    if raw_servers is None:
        raw_servers = {}
    if not isinstance(raw_servers, dict):
        return McpFile(
            path=path, exists=True, text=text, error=f"`{SERVERS_KEY}` is not an object"
        )

    servers: dict[str, McpServer] = {}
    invalid: dict[str, str] = {}
    for name, entry in raw_servers.items():
        try:
            servers[name] = parse_server(entry)
        except McpFileError as exc:
            invalid[name] = str(exc)

    return McpFile(
        path=path,
        exists=True,
        text=text,
        document=document,
        servers=servers,
        invalid=invalid,
    )


def write_document(path: Path, document: dict[str, Any], *, previous: str = "") -> str:
    """Write the document atomically, keeping the file's own line endings.

    Write-then-rename, like the docs layer: a crash mid-write leaves the previous file
    intact rather than a truncated one the CLI would then fail to load.
    """
    newline = "\r\n" if "\r\n" in previous else "\n"
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline=newline)
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise McpFileError(f"could not write {path.name}: {exc}") from exc
    return text


def write_text(path: Path, text: str) -> None:
    """Write raw JSON the user typed, after checking it is a usable document.

    The bytes are kept exactly as given — this is the escape hatch for anything the form
    cannot express, and reformatting someone's file underneath them defeats it.
    """
    try:
        document = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise McpFileError(f"line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise McpFileError("the file must be a JSON object")
    servers = document.get(SERVERS_KEY, {})
    if servers is not None and not isinstance(servers, dict):
        raise McpFileError(f"`{SERVERS_KEY}` is not an object")

    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="")
        tmp.replace(path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise McpFileError(f"could not write {path.name}: {exc}") from exc


def check_name(name: str) -> str:
    name = name.strip()
    if not NAME_PATTERN.match(name):
        raise McpFileError(
            "a server name may contain letters, digits, '_', '.' and '-' only — it "
            "becomes part of every tool name the server exposes"
        )
    return name


def upsert_server(file: McpFile, name: str, server: McpServer) -> dict[str, Any]:
    """Return the document with ``name`` added or replaced.

    Keys the Studio does not model are kept from the existing entry, so editing the
    command of a server that also carries some future field does not delete that field.
    """
    file.require_writable()
    document = dict(file.document)
    servers = dict(file.raw_servers)

    payload = server_payload(server)
    existing = servers.get(name)
    if isinstance(existing, dict):
        preserved = {k: v for k, v in existing.items() if k not in KNOWN_SERVER_KEYS}
        payload = {**preserved, **payload}

    servers[name] = payload
    document[SERVERS_KEY] = servers
    return document


def remove_server(file: McpFile, name: str) -> dict[str, Any]:
    file.require_writable()
    servers = dict(file.raw_servers)
    if name not in servers:
        raise KeyError(name)
    del servers[name]
    document = dict(file.document)
    document[SERVERS_KEY] = servers
    return document


# ---------------------------------------------------------------- agent-facing view


def enabled_servers(paths: ProjectPaths, config: AppConfig) -> dict[str, dict[str, Any]]:
    """The servers to hand an agent session, keyed by name (§10.7).

    Invalid entries are skipped rather than passed through: the CLI would reject the
    whole config, taking the Studio's own tools down with it.
    """
    file = read_mcp_file(paths.mcp_file)
    if file.error is not None:
        log.warning("Ignoring %s: %s", file.path, file.error)
        return {}
    disabled = set(config.mcp.disabled)
    return {
        name: server_payload(server)
        for name, server in file.servers.items()
        if name not in disabled
    }


def allowed_tool_names(servers: dict[str, Any]) -> list[str]:
    """``mcp__<server>`` allows every tool of that server without a permission prompt.

    An agent session has no one to answer a prompt, so a server the user enabled but
    whose tools are not allowed is a server whose every call is denied. Enabling it on
    the page is the consent step.
    """
    return [f"mcp__{name}" for name in servers]


def command_status(server: McpServer) -> tuple[bool | None, str]:
    """Whether a stdio server's command is on PATH — the one check worth doing eagerly.

    Remote servers get ``None``: reaching out to a URL to see whether it answers is a
    network call the Studio has no business making while rendering a settings page.
    """
    if isinstance(server, RemoteServer):
        return None, server.url
    found = shutil.which(server.command)
    if found is None:
        return False, f"`{server.command}` was not found on PATH"
    return True, found

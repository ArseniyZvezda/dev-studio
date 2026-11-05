"""MCP servers for the project's Claude agents (§10.7, §11.1).

Every endpoint answers with the same whole-page payload, including the ones that only
change one server: the file is edited elsewhere too — by hand, by the `claude` CLI, by an
agent — so a client that patched its own state from a narrow response would drift from
the file within one edit.

**One of the servers is not in the file.** The Studio's own tools (§10.4) are an in-process
MCP server built out of the running orchestrator, so there is nothing in `.mcp.json` to
describe them and never should be — writing an entry there would tell the developer's
terminal to launch a Studio it cannot launch. It is listed here anyway, because a page
called "MCP Servers" that omits the server every agent actually uses is lying by omission:
it arrives marked `builtin`, carrying the tool names it provides, and the delete and edit
routes refuse it. Disabling it is allowed and goes through the same `config.mcp.disabled`
as any other server — `build_toolset` reads that list — because "this project's agents work
with files only" is a coherent thing to want, and the run already has a branch for a member
with no Studio tools.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import mcp as mcp_config
from ..core.context import AppContext
from ..core.mcp import McpFileError
from ..teams.tools import SERVER_NAME as BUILTIN_SERVER
from .deps import Ctx

router = APIRouter(prefix="/mcp", tags=["mcp"])

#: What the built-in server offers, in the order `build_toolset` registers them. Listed
#: statically rather than by building a toolset: that needs a member and a task, and the
#: page is answering "what is installed", not "what would this run get".
BUILTIN_TOOLS = [
    "list_team",
    "list_tasks",
    "get_task",
    "create_task",
    "update_task",
    "add_task_note",
    "finish_task",
    "list_docs",
    "read_doc",
    "write_doc",
    "spawn_subagent",
    "list_subagents",
    "get_subagent",
    "wait_subagents",
    "stop_subagent",
    "send_subagent_message",
    "list_node_types",
    "list_pipelines",
    "read_pipeline",
    "validate_pipeline",
    "create_pipeline",
    "write_pipeline",
    "list_automations",
    "read_automation",
    "create_automation",
    "write_automation",
    "list_skills",
    "read_skill",
    "create_skill",
    "write_skill",
]


def _builtin_entry(ctx: AppContext) -> dict[str, Any]:
    """The Studio's own server, in the same shape as an entry read out of the file."""
    return {
        "name": BUILTIN_SERVER,
        "type": "builtin",
        "builtin": True,
        "enabled": BUILTIN_SERVER not in ctx.config.mcp.disabled,
        "available": True,
        "detail": "in-process — tasks, documents, pipelines, automations and skills",
        "tool_prefix": f"mcp__{BUILTIN_SERVER}__",
        "tools": BUILTIN_TOOLS,
        "args": [],
        "env": {},
        "headers": {},
    }


def _reject_builtin(name: str, verb: str) -> None:
    if name == BUILTIN_SERVER:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{BUILTIN_SERVER!r} is the Studio's own built-in server. It is not in "
                f".mcp.json, so there is nothing to {verb} — switch it off instead if its "
                "agents should work without the task board and documents."
            ),
        )


class ServerInput(BaseModel):
    """One entry of ``mcpServers``, in the shape the form produces.

    Deliberately flat rather than a discriminated union: ``type`` is optional in the
    file format, and the same inference that reads a hand-written entry (§ ``parse_server``)
    should apply to one the page submits.
    """

    type: Literal["stdio", "http", "sse"] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    headers: dict[str, str] | None = None


class FileInput(BaseModel):
    content: str


class SettingsInput(BaseModel):
    strict: bool | None = None


def _overview(ctx: AppContext) -> dict[str, Any]:
    file = mcp_config.read_mcp_file(ctx.paths.mcp_file)
    disabled = set(ctx.config.mcp.disabled)

    # First, so the server every agent actually uses heads the list — and so a file entry
    # that happens to share its name cannot take its key in the client's `v-for`.
    servers: list[dict[str, Any]] = [_builtin_entry(ctx)]
    shadowed: list[dict[str, str]] = []
    for name, server in file.servers.items():
        if name == BUILTIN_SERVER:
            # The orchestrator merges the Studio's tools *over* the project's, so an entry
            # named `voxarium_studio` genuinely never reaches a session. Reported rather than
            # rendered as a second server with the same name.
            shadowed.append(
                {
                    "name": name,
                    "error": "shadowed by the Studio's own built-in server of the same name",
                }
            )
            continue
        available, detail = mcp_config.command_status(server)
        entry: dict[str, Any] = {
            "name": name,
            "builtin": False,
            "enabled": name not in disabled,
            "available": available,
            "detail": detail,
            "tool_prefix": f"mcp__{name}__",
            "tools": [],
            **mcp_config.server_payload(server),
        }
        entry.setdefault("args", [])
        entry.setdefault("env", {})
        entry.setdefault("headers", {})
        servers.append(entry)

    return {
        "path": str(file.path),
        "exists": file.exists,
        "error": file.error,
        "text": file.text,
        "strict": ctx.config.mcp.strict,
        "servers": servers,
        "invalid": [{"name": name, "error": reason} for name, reason in file.invalid.items()]
        + shadowed,
    }


@router.get("")
async def get_mcp(ctx: Ctx) -> dict[str, Any]:
    return _overview(ctx)


@router.put("/servers/{name}")
async def upsert_server(name: str, payload: ServerInput, ctx: Ctx) -> dict[str, Any]:
    """Add a server or replace one, keeping any fields the Studio does not model."""
    # Refused rather than written and then shown as shadowed: the page would offer to
    # create an entry that can never reach a session, which is a worse answer than "no".
    _reject_builtin(name, "replace")
    try:
        checked = mcp_config.check_name(name)
        server = mcp_config.parse_server(payload.model_dump(exclude_none=True))
        file = mcp_config.read_mcp_file(ctx.paths.mcp_file)
        document = mcp_config.upsert_server(file, checked, server)
        mcp_config.write_document(ctx.paths.mcp_file, document, previous=file.text)
    except McpFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _overview(ctx)


@router.delete("/servers/{name}")
async def delete_server(name: str, ctx: Ctx) -> dict[str, Any]:
    # The UI offers no delete on the built-in entry; this is the half that holds when
    # someone calls the endpoint directly, which is the half that actually enforces it.
    _reject_builtin(name, "remove")
    file = mcp_config.read_mcp_file(ctx.paths.mcp_file)
    try:
        document = mcp_config.remove_server(file, name)
        mcp_config.write_document(ctx.paths.mcp_file, document, previous=file.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"No MCP server named {name!r}") from exc
    except McpFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A name that is gone from the file has nothing left to disable; leaving it in
    # config.yaml would silently switch off a future server that reused the name.
    if name in ctx.config.mcp.disabled:
        ctx.config.mcp.disabled = [item for item in ctx.config.mcp.disabled if item != name]
        ctx.save_config()
    return _overview(ctx)


@router.post("/servers/{name}/{action}")
async def set_server_enabled(
    name: str, action: Literal["enable", "disable"], ctx: Ctx
) -> dict[str, Any]:
    """Withhold a server from agent sessions without removing it from ``.mcp.json``.

    The built-in server takes this route too, and that is the coherent half of "built-in":
    it cannot be deleted because there is no file entry to delete, but ``mcp.disabled`` is
    a Studio-side list and works on it unchanged — a project whose agents should touch
    files and nothing else is a real configuration, not a mistake.
    """
    if name != BUILTIN_SERVER:
        file = mcp_config.read_mcp_file(ctx.paths.mcp_file)
        if name not in file.raw_servers:
            raise HTTPException(status_code=404, detail=f"No MCP server named {name!r}")

    disabled = [item for item in ctx.config.mcp.disabled if item != name]
    if action == "disable":
        disabled.append(name)
    ctx.config.mcp.disabled = disabled
    ctx.save_config()
    return _overview(ctx)


@router.put("/file")
async def write_file(payload: FileInput, ctx: Ctx) -> dict[str, Any]:
    """Write the file verbatim — the escape hatch for anything the form cannot express."""
    try:
        mcp_config.write_text(ctx.paths.mcp_file, payload.content)
    except McpFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _overview(ctx)


@router.patch("/settings")
async def update_settings(payload: SettingsInput, ctx: Ctx) -> dict[str, Any]:
    if payload.strict is not None:
        ctx.config.mcp.strict = payload.strict
        ctx.save_config()
    return _overview(ctx)

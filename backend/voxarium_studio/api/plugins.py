"""Plugin management (§9.4, §11.1)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..core.config import PluginConfig
from ..plugins.envs import EnvError, ensure_env
from ..plugins.registry import FilePreviewer, Registry, SyntaxGrammar
from .deps import Ctx

log = logging.getLogger(__name__)
router = APIRouter(prefix="/plugins", tags=["plugins"])


class PluginSettingsUpdate(BaseModel):
    settings: dict[str, Any]


@router.get("")
async def list_plugins(ctx: Ctx) -> list[dict[str, Any]]:
    return [plugin.to_json() for plugin in ctx.registry.plugins()]


@router.get("/pages")
async def list_pages(ctx: Ctx) -> list[dict[str, Any]]:
    """Menu entries contributed by plugins, mounted into the router at runtime (§9.3)."""
    return [
        {
            "id": page.id,
            "plugin_id": page.plugin_id,
            "title": page.title,
            "route": page.route,
            "icon": page.icon,
            "component_url": (
                ctx.registry.asset_url(page.plugin_id, page.component) if page.component else None
            ),
        }
        for page in ctx.registry.pages()
    ]


def previewer_json(previewer: FilePreviewer, registry: Registry) -> dict[str, Any]:
    """Wire form of a previewer; also embedded in the Explorer preview payload.

    The registry is what stamps the asset's version into `component_url`, so this cannot
    be built from the previewer alone.
    """
    return {
        "id": previewer.id,
        "plugin_id": previewer.plugin_id,
        "label": previewer.label,
        "extensions": list(previewer.extensions),
        "component_url": registry.asset_url(previewer.plugin_id, previewer.component),
    }


@router.get("/previewers")
async def list_previewers(ctx: Ctx) -> list[dict[str, Any]]:
    """File types plugins have claimed in the Explorer preview pane (§6.2)."""
    return [previewer_json(previewer, ctx.registry) for previewer in ctx.registry.previewers()]


def grammar_json(grammar: SyntaxGrammar, registry: Registry) -> dict[str, Any]:
    """Wire form of a syntax grammar.

    As with a previewer, the registry is what stamps the file's mtime and size into
    `grammar_url` — an unstamped URL is one a browser is free to answer from its own
    cache with the copy it fetched before the grammar was edited.
    """
    return {
        "id": grammar.id,
        "plugin_id": grammar.plugin_id,
        "name": grammar.name,
        "extensions": list(grammar.extensions),
        "scope_name": grammar.scope_name,
        "aliases": list(grammar.aliases),
        "grammar_url": registry.asset_url(grammar.plugin_id, grammar.grammar),
    }


@router.get("/grammars")
async def list_grammars(ctx: Ctx) -> list[dict[str, Any]]:
    """Languages plugins have taught the Explorer to highlight (§6.2).

    The payload names a TextMate grammar and where to fetch it; the frontend hands that
    to Shiki. Two plugins may claim the same suffix — both are listed, and choosing
    between them is the frontend's decision, not the registry's.
    """
    return [grammar_json(grammar, ctx.registry) for grammar in ctx.registry.grammars()]


@router.post("/{plugin_id}/enable")
async def enable_plugin(plugin_id: str, ctx: Ctx) -> dict[str, Any]:
    return await _set_enabled(ctx, plugin_id, True)


@router.post("/{plugin_id}/disable")
async def disable_plugin(plugin_id: str, ctx: Ctx) -> dict[str, Any]:
    return await _set_enabled(ctx, plugin_id, False)


async def _set_enabled(ctx: Ctx, plugin_id: str, enabled: bool) -> dict[str, Any]:
    if ctx.registry.plugin(plugin_id) is None:
        raise HTTPException(status_code=404, detail=f"No plugin {plugin_id!r}")
    state = ctx.config.plugins.setdefault(plugin_id, PluginConfig())
    state.enabled = enabled
    ctx.save_config()
    await ctx.reload_plugins()
    plugin = ctx.registry.plugin(plugin_id)
    return plugin.to_json() if plugin else {"id": plugin_id, "enabled": enabled}


@router.put("/{plugin_id}/settings")
async def update_plugin_settings(
    plugin_id: str, payload: PluginSettingsUpdate, ctx: Ctx
) -> dict[str, Any]:
    if ctx.registry.plugin(plugin_id) is None:
        raise HTTPException(status_code=404, detail=f"No plugin {plugin_id!r}")
    state = ctx.config.plugins.setdefault(plugin_id, PluginConfig())
    state.settings = payload.settings
    ctx.save_config()
    # A plugin's `register()` is handed its settings, so it can shape what it contributes
    # from them — port defaults, which node types to offer at all. Saving therefore has to
    # re-run it, the same way enable/disable does, or the change only lands on restart.
    await ctx.reload_plugins()
    return {"id": plugin_id, "settings": state.settings}


@router.get("/{plugin_id}/settings")
async def get_plugin_settings(plugin_id: str, ctx: Ctx) -> dict[str, Any]:
    state = ctx.config.plugins.get(plugin_id)
    return {"id": plugin_id, "settings": state.settings if state else {}}


@router.post("/{plugin_id}/actions/{action_id}")
async def run_plugin_action(
    plugin_id: str,
    action_id: str,
    ctx: Ctx,
    payload: dict[str, Any] | None = Body(default=None),
) -> Any:
    """Call an action the plugin registered — its own endpoint, in effect (§9.7).

    The JSON body is the handler's ``payload`` and whatever it returns is the response, as
    is: a plugin page reads its own shapes, so there is nothing for the Studio to wrap. A
    ``ValueError`` is the handler saying the request was wrong, hence the 400.
    """
    action = ctx.registry.action(plugin_id, action_id)
    if action is None:
        if ctx.registry.plugin(plugin_id) is None:
            raise HTTPException(status_code=404, detail=f"No plugin {plugin_id!r}")
        raise HTTPException(
            status_code=404, detail=f"Plugin {plugin_id!r} has no action {action_id!r}"
        )
    try:
        return await action.handler(ctx.plugin_host(plugin_id), dict(payload or {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{plugin_id}/install-env")
async def install_plugin_env(plugin_id: str, ctx: Ctx, force: bool = False) -> dict[str, Any]:
    """Provision the plugin's isolated venv with uv (§2.1).

    Kept off the start-up path deliberately: a plugin pinning torch can take minutes,
    and the Studio must still boot in the meantime.
    """
    plugin = ctx.registry.plugin(plugin_id)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"No plugin {plugin_id!r}")

    lines: list[str] = []

    async def log_fn(line: str) -> None:
        lines.append(line)

    try:
        python_path = await ensure_env(plugin.manifest, force=force, log_fn=log_fn)
    except EnvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if python_path is None:
        return {
            "id": plugin_id,
            "provisioned": False,
            "detail": "This plugin does not declare an isolated environment.",
            "log": lines,
        }
    return {
        "id": plugin_id,
        "provisioned": True,
        "python": str(python_path),
        "log": lines[-200:],
    }


@router.get("/{plugin_id}/assets/{asset_path:path}")
async def get_plugin_asset(plugin_id: str, asset_path: str, ctx: Ctx) -> FileResponse:
    """Serve a plugin's frontend bundle so the SPA can import it at runtime (§9.3)."""
    plugin = ctx.registry.plugin(plugin_id)
    if plugin is None or plugin.manifest.root is None:
        raise HTTPException(status_code=404, detail=f"No plugin {plugin_id!r}")

    root = plugin.manifest.root.resolve()
    target = (root / asset_path).resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=403, detail="Asset path escapes the plugin directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"No asset {asset_path!r}")
    if target.suffix not in {".js", ".mjs", ".css", ".json", ".svg", ".png", ".webp", ".woff2"}:
        raise HTTPException(status_code=403, detail="Asset type is not servable")

    media_type = "text/javascript" if target.suffix in {".js", ".mjs"} else None
    # `no-cache` means "store it, but revalidate before every use" — not "do not store".
    # Without it there is no explicit freshness, so a browser is free to invent one
    # heuristically and serve a stale module; editing a plugin's frontend and reloading is
    # the documented way to see the change, and that has to be true rather than usually
    # true. `FileResponse` already sends an ETag, so revalidation costs a 304.
    return FileResponse(
        target, media_type=media_type, headers={"cache-control": "no-cache, must-revalidate"}
    )

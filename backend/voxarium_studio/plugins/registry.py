"""Registry of loaded plugins, node types and plugin-contributed pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..schemas.nodes import NodeTypeSchema
from .api import BaseNode
from .manifest import PluginManifest

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PluginHook:
    """A function a plugin asked to have run on something happening (§9.7).

    ``event`` is an :class:`~voxarium_studio.ws.events.EventType` value, or ``None`` for the one
    trigger that is not a bus event: the Studio opening the project. The two are kept apart
    on purpose — ``project.opened`` is also published on the bus, to browsers, when a
    project is switched, and a plugin that asked to run "when the project opens" must run
    exactly once for it.
    """

    plugin_id: str
    id: str
    event: str | None
    handler: Any


@dataclass(slots=True)
class PluginAction:
    """``POST /api/plugins/{plugin_id}/actions/{id}`` — a plugin's own endpoint (§9.7)."""

    plugin_id: str
    id: str
    handler: Any


@dataclass(slots=True)
class SearchProvider:
    """An answerer for the search field in the rail (§6.1, §9.7)."""

    plugin_id: str
    id: str
    handler: Any
    label: str | None = None

    @property
    def key(self) -> str:
        """Unique across plugins — two plugins may both call their provider ``code``."""
        return f"{self.plugin_id}.{self.id}"


@dataclass(slots=True)
class AutomationAction:
    """Something a plugin offers the Automations page as an action (§6.5, §9.7).

    Its own list rather than the plugin's ``actions``: those are a settings page's endpoints
    — ``graphify`` has a ``reset`` among them that deletes an index — and what may run
    unattended, on a trigger nobody is watching, is the plugin's to say one by one.
    """

    plugin_id: str
    id: str
    handler: Any
    label: str
    description: str | None = None

    @property
    def key(self) -> str:
        return f"{self.plugin_id}.{self.id}"


@dataclass(slots=True)
class PluginPage:
    """A menu entry contributed by a plugin, mounted into Vue Router at runtime (§9.3)."""

    plugin_id: str
    id: str
    title: str
    route: str
    icon: str | None = None
    component: str | None = None
    """URL the frontend dynamically imports; ``None`` renders the built-in settings page."""


@dataclass(slots=True)
class FilePreviewer:
    """A viewer a plugin contributes for a file type, mounted by the Explorer (§6.2).

    3D preview is deliberately not built in (§14); this is the seam the spec reserved
    for it, so a `.fbx` or `.vox` viewer is an installable plugin rather than a fork.
    """

    plugin_id: str
    id: str
    extensions: tuple[str, ...]
    component: str
    """Module path inside the plugin, served from ``/api/plugins/{id}/assets/…``."""

    label: str | None = None
    priority: int = 0


@dataclass(slots=True)
class SyntaxGrammar:
    """A language a plugin taught the Studio to highlight (§6.2).

    The Studio ships no grammars of its own, for the same reason it ships no 3D viewer:
    a TextMate grammar is *data* in the format VS Code already uses, so highlighting a
    new language is an installable plugin carrying one JSON file rather than a fork.
    """

    plugin_id: str
    id: str
    name: str
    extensions: tuple[str, ...]
    scope_name: str
    """Root scope the grammar declares, e.g. ``source.angelscript``."""

    grammar: str
    """Path to the grammar file inside the plugin, served from ``/api/plugins/{id}/assets/…``."""

    aliases: tuple[str, ...] = ()


def plugin_asset_url(plugin_id: str, asset: str, root: Path | None = None) -> str:
    """URL the SPA dynamically imports a plugin's frontend module from (§9.3).

    Given the plugin's root, the file's mtime and size are stamped into a `v=` parameter
    so the URL itself changes whenever the asset does. That is what actually keeps a
    browser off a stale module: `Cache-Control: no-cache` asks it to revalidate, but a
    browser that already considers its copy fresh never asks at all, and no header sent
    later can reach back and correct that. A URL it has never seen has nothing to serve
    from cache.
    """
    url = f"/api/plugins/{plugin_id}/assets/{asset}"
    if root is None:
        return url
    try:
        stat = (root / asset).stat()
    except OSError:
        # A missing asset is the endpoint's 404 to report, not this function's problem.
        return url
    return f"{url}?v={int(stat.st_mtime)}-{stat.st_size}"


@dataclass(slots=True)
class LoadedPlugin:
    manifest: PluginManifest
    enabled: bool = True
    status: str = "loaded"
    """``loaded`` | ``disabled`` | ``error``."""

    error: str | None = None
    node_type_ids: list[str] = field(default_factory=list)
    page_ids: list[str] = field(default_factory=list)
    previewer_ids: list[str] = field(default_factory=list)
    grammar_ids: list[str] = field(default_factory=list)
    hook_ids: list[str] = field(default_factory=list)
    action_ids: list[str] = field(default_factory=list)
    search_provider_ids: list[str] = field(default_factory=list)
    automation_action_ids: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.manifest.id

    def to_json(self) -> dict[str, Any]:
        m = self.manifest
        return {
            "id": m.id,
            "name": m.name,
            "version": m.version,
            "author": m.author,
            "description": m.description,
            "scope": m.scope,
            "enabled": self.enabled,
            "status": self.status,
            "error": self.error,
            "node_types": list(self.node_type_ids),
            "pages": list(self.page_ids),
            "previewers": list(self.previewer_ids),
            "grammars": list(self.grammar_ids),
            "hooks": list(self.hook_ids),
            "actions": list(self.action_ids),
            "search_providers": list(self.search_provider_ids),
            "automation_actions": list(self.automation_action_ids),
            "has_backend": m.backend is not None and m.backend.entry is not None,
            "has_venv": m.venv_python() is not None,
            "root": str(m.root) if m.root else None,
        }


class Registry:
    """Single source of truth for what the running Studio can execute and display."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._plugins: dict[str, LoadedPlugin] = {}
        self._node_types: dict[str, NodeTypeSchema] = {}
        self._node_classes: dict[str, type[BaseNode]] = {}
        self._pages: dict[str, PluginPage] = {}
        self._previewers: dict[str, FilePreviewer] = {}
        self._grammars: dict[str, SyntaxGrammar] = {}
        self._hooks: dict[str, PluginHook] = {}
        self._actions: dict[str, PluginAction] = {}
        self._search_providers: dict[str, SearchProvider] = {}
        self._automation_actions: dict[str, AutomationAction] = {}
        self._current_plugin: str | None = None

    # ---- registration (called from PluginRegistry during plugin load) ----

    def register_node_type(
        self, schema: NodeTypeSchema, node_cls: type[BaseNode] | None
    ) -> None:
        if schema.id in self._node_types:
            existing = self._node_types[schema.id]
            if existing.plugin_id != schema.plugin_id:
                raise ValueError(
                    f"Node type {schema.id!r} is already registered by plugin "
                    f"{existing.plugin_id!r}"
                )
            log.debug("Re-registering node type %s", schema.id)
        self._node_types[schema.id] = schema
        if node_cls is not None:
            self._node_classes[schema.id] = node_cls
        plugin = self._plugins.get(schema.plugin_id)
        if plugin is not None and schema.id not in plugin.node_type_ids:
            plugin.node_type_ids.append(schema.id)

    def register_page(
        self,
        *,
        plugin_id: str,
        id: str,
        title: str,
        route: str,
        icon: str | None = None,
        component: str | None = None,
    ) -> None:
        page = PluginPage(
            plugin_id=plugin_id,
            id=id,
            title=title,
            route=route,
            icon=icon,
            component=component,
        )
        self._pages[id] = page
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.page_ids:
            plugin.page_ids.append(id)

    def register_previewer(
        self,
        *,
        plugin_id: str,
        id: str,
        extensions: list[str] | tuple[str, ...],
        component: str,
        label: str | None = None,
        priority: int = 0,
    ) -> None:
        previewer = FilePreviewer(
            plugin_id=plugin_id,
            id=id,
            extensions=tuple(extensions),
            component=component,
            label=label,
            priority=priority,
        )
        self._previewers[id] = previewer
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.previewer_ids:
            plugin.previewer_ids.append(id)

    def register_grammar(
        self,
        *,
        plugin_id: str,
        id: str,
        name: str,
        extensions: list[str] | tuple[str, ...],
        scope_name: str,
        grammar: str,
        aliases: list[str] | tuple[str, ...] = (),
    ) -> None:
        """Teach the Explorer to highlight a language (§6.2).

        Grammars live on the registry so disabling a plugin withdraws its grammars.
        A grammar surviving a reload would leave the Explorer colouring a file type from
        a file nothing serves any more.
        """
        syntax = SyntaxGrammar(
            plugin_id=plugin_id,
            id=id,
            name=name,
            extensions=tuple(extensions),
            scope_name=scope_name,
            grammar=grammar,
            aliases=tuple(aliases),
        )
        self._grammars[id] = syntax
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.grammar_ids:
            plugin.grammar_ids.append(id)

    def register_hook(self, *, plugin_id: str, id: str, event: str | None, handler: Any) -> None:
        """Ask for ``handler`` to run on an event, or on the project opening (§9.7).

        On the registry, like everything else a plugin contributes, so disabling the plugin
        withdraws the hook: the context reads the *current* registry's hooks for every
        event, and a hook from a plugin that is no longer loaded is simply not there.
        """
        key = f"{plugin_id}:{id}"
        self._hooks[key] = PluginHook(plugin_id=plugin_id, id=id, event=event, handler=handler)
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.hook_ids:
            plugin.hook_ids.append(id)

    def register_action(self, *, plugin_id: str, id: str, handler: Any) -> None:
        self._actions[f"{plugin_id}:{id}"] = PluginAction(plugin_id=plugin_id, id=id, handler=handler)
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.action_ids:
            plugin.action_ids.append(id)

    def register_search_provider(
        self, *, plugin_id: str, id: str, handler: Any, label: str | None = None
    ) -> None:
        provider = SearchProvider(plugin_id=plugin_id, id=id, handler=handler, label=label)
        self._search_providers[provider.key] = provider
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.search_provider_ids:
            plugin.search_provider_ids.append(id)

    def register_automation_action(
        self,
        *,
        plugin_id: str,
        id: str,
        handler: Any,
        label: str,
        description: str | None = None,
    ) -> None:
        action = AutomationAction(
            plugin_id=plugin_id, id=id, handler=handler, label=label, description=description
        )
        self._automation_actions[action.key] = action
        plugin = self._plugins.get(plugin_id)
        if plugin is not None and id not in plugin.automation_action_ids:
            plugin.automation_action_ids.append(id)

    def add_plugin(self, plugin: LoadedPlugin) -> None:
        self._plugins[plugin.id] = plugin

    def remove_plugin(self, plugin_id: str) -> None:
        """Drop a plugin and everything it contributed."""
        plugin = self._plugins.pop(plugin_id, None)
        if plugin is None:
            return
        for node_type_id in plugin.node_type_ids:
            self._node_types.pop(node_type_id, None)
            self._node_classes.pop(node_type_id, None)
        for page_id in plugin.page_ids:
            self._pages.pop(page_id, None)
        for previewer_id in plugin.previewer_ids:
            self._previewers.pop(previewer_id, None)
        for grammar_id in plugin.grammar_ids:
            self._grammars.pop(grammar_id, None)
        for hook_id in plugin.hook_ids:
            self._hooks.pop(f"{plugin_id}:{hook_id}", None)
        for action_id in plugin.action_ids:
            self._actions.pop(f"{plugin_id}:{action_id}", None)
        for provider_id in plugin.search_provider_ids:
            self._search_providers.pop(f"{plugin_id}.{provider_id}", None)
        for action_id in plugin.automation_action_ids:
            self._automation_actions.pop(f"{plugin_id}.{action_id}", None)

    # ---- lookups ----

    def node_type(self, node_type_id: str) -> NodeTypeSchema | None:
        return self._node_types.get(node_type_id)

    def node_class(self, node_type_id: str) -> type[BaseNode] | None:
        return self._node_classes.get(node_type_id)

    def node_types(self) -> list[NodeTypeSchema]:
        return sorted(self._node_types.values(), key=lambda s: (s.category, s.name))

    def plugin(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def asset_url(self, plugin_id: str, asset: str) -> str:
        """`plugin_asset_url` with the plugin's root filled in, so the URL carries its
        version stamp. Everything the SPA imports should come through here."""
        plugin = self.plugin(plugin_id)
        return plugin_asset_url(plugin_id, asset, plugin.manifest.root if plugin else None)

    def plugins(self) -> list[LoadedPlugin]:
        return sorted(self._plugins.values(), key=lambda p: p.id)

    def pages(self) -> list[PluginPage]:
        return sorted(self._pages.values(), key=lambda p: p.title)

    def previewers(self) -> list[FilePreviewer]:
        return sorted(self._previewers.values(), key=lambda p: (-p.priority, p.id))

    def previewer_for(self, suffix: str) -> FilePreviewer | None:
        """Best previewer for a file suffix, or ``None`` to fall back to the built-in panel."""
        wanted = suffix.lower()
        if not wanted.startswith("."):
            wanted = f".{wanted}"
        return next((p for p in self.previewers() if wanted in p.extensions), None)

    def grammars(self) -> list[SyntaxGrammar]:
        """Every language plugins have taught the Studio to highlight (§6.2).

        Two plugins may claim the same suffix; both are returned and the frontend decides
        which one it loads, exactly as it decides between two previewers.
        """
        return sorted(self._grammars.values(), key=lambda g: g.id)

    def project_opened_hooks(self) -> list[PluginHook]:
        """Hooks for the Studio opening the project, in registration order."""
        return [hook for hook in self._hooks.values() if hook.event is None]

    def event_hooks(self, event_type: str) -> list[PluginHook]:
        return [hook for hook in self._hooks.values() if hook.event == event_type]

    def hooks(self) -> list[PluginHook]:
        return list(self._hooks.values())

    def action(self, plugin_id: str, action_id: str) -> PluginAction | None:
        return self._actions.get(f"{plugin_id}:{action_id}")

    def actions(self) -> list[PluginAction]:
        return sorted(self._actions.values(), key=lambda a: (a.plugin_id, a.id))

    def search_providers(self) -> list[SearchProvider]:
        """Every answerer for the rail's search field, in a stable order (§6.1)."""
        return sorted(self._search_providers.values(), key=lambda p: p.key)

    def automation_action(self, plugin_id: str, action_id: str) -> AutomationAction | None:
        return self._automation_actions.get(f"{plugin_id}.{action_id}")

    def automation_actions(self) -> list[AutomationAction]:
        """What plugins offer the Automations page, in a stable order (§6.5)."""
        return sorted(self._automation_actions.values(), key=lambda a: a.key)

    def plugin_root(self, plugin_id: str) -> Path:
        plugin = self._plugins.get(plugin_id)
        if plugin is None or plugin.manifest.root is None:
            raise KeyError(f"Unknown plugin {plugin_id!r}")
        return plugin.manifest.root

    def manifest_for_node_type(self, node_type_id: str) -> PluginManifest | None:
        schema = self._node_types.get(node_type_id)
        if schema is None:
            return None
        plugin = self._plugins.get(schema.plugin_id)
        return plugin.manifest if plugin else None

    def clear(self) -> None:
        self._plugins.clear()
        self._node_types.clear()
        self._node_classes.clear()
        self._pages.clear()
        self._previewers.clear()
        self._grammars.clear()
        self._hooks.clear()
        self._actions.clear()
        self._search_providers.clear()
        self._automation_actions.clear()

"""Public plugin API (§9.2).

Plugins import from :mod:`voxarium_studio.plugin_api`, which re-exports this module. Keeping
the surface small and re-exported through one name is what lets the internals move
without breaking installed plugins.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..core.config import AppConfig, effective_launch_root
from ..core.paths import ProjectPaths
from ..schemas.nodes import ExecutionSpec, NodeTypeSchema, PortSchema
from ..ws.bus import Event, EventBus
from ..ws.events import EventType

if TYPE_CHECKING:  # pragma: no cover
    import asyncio

    from .registry import Registry

log = logging.getLogger(__name__)


class PluginHost:
    """What a plugin's hooks, actions and search providers are handed when they run (§9.7).

    Built by the :class:`~voxarium_studio.core.context.AppContext` at call time — never at
    ``register()`` — so everything on it is *live*: ``settings`` is what ``config.yaml``
    says now, ``launch_root`` is where an agent session would start now. A plugin keeps
    nothing of it between calls; what has to survive goes in :attr:`state`.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        paths: ProjectPaths,
        config: Callable[[], AppConfig],
        bus: EventBus,
        state: dict[str, Any],
        spawn: Callable[[Coroutine[Any, Any, Any], str], "asyncio.Task[Any]"],
    ) -> None:
        self.plugin_id = plugin_id
        self._paths = paths
        self._config = config
        self._bus = bus
        self.state = state
        """Runtime state that outlives a reload of the plugin's code.

        Saving a plugin's settings re-runs its ``register()`` against a freshly imported
        module, so a module-level variable is a new one afterwards — and a lock guarding a
        subprocess would be a new, unlocked lock while the subprocess is still running. This
        dict is the context's, keyed by plugin id, and the same one is handed over every
        time until the project is closed. Keep plain objects in it and never type-check
        them: after a reload the class they were built from is a different class.
        """
        self._spawn = spawn

    @property
    def project_root(self) -> Path:
        return self._paths.root

    @property
    def data_dir(self) -> Path:
        """The managed project's ``.voxarium_studio/`` directory."""
        return self._paths.data

    @property
    def config(self) -> AppConfig:
        return self._config()

    @property
    def settings(self) -> dict[str, Any]:
        """This plugin's per-project settings — what its settings page saved."""
        state = self.config.plugins.get(self.plugin_id)
        return dict(state.settings) if state else {}

    @property
    def launch_root(self) -> Path:
        """Where an agent session starts (``teams.launch_root``, §10.5), as it would today.

        Absolute, and always a directory: a configured root missing on this machine falls
        back to the project root, exactly as the orchestrator does when it starts a run.
        """
        return effective_launch_root(self._paths.root, self.config.teams.launch_root)

    async def publish(self, name: str, data: dict[str, Any] | None = None) -> None:
        """Tell every connected browser something, as a ``plugin.event`` (§11.2).

        The plugin's frontend subscribes to that one type and filters on ``plugin_id`` and
        ``name``; the payload is whatever the plugin puts in ``data``.
        """
        await self._bus.publish(
            EventType.PLUGIN_EVENT,
            {"plugin_id": self.plugin_id, "name": name, "data": dict(data or {})},
        )

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str = "") -> "asyncio.Task[Any]":
        """Run a coroutine in the background under the Studio's supervision.

        Preferred over ``asyncio.create_task`` from inside a plugin because the task is
        then the context's: it is cancelled when the project is closed or the server stops,
        and an exception in it is logged against the plugin rather than lost with the loop's
        "task exception was never retrieved". An action that starts long work returns at
        once and leaves the work here.
        """
        return self._spawn(coro, name or f"plugin:{self.plugin_id}")


class AutomationCancelled(Exception):
    """Raised by an automation action whose work was stopped before it finished — a run
    somebody cancelled, a queued update a Cancel dropped. The firing is recorded as cancelled
    rather than failed, with the message as its line in the log (§6.5)."""


@dataclass(slots=True)
class SearchQuery:
    """What the rail's search field asked (§6.1)."""

    text: str
    limit: int = 20
    deep: bool = False
    """The user pressed Enter rather than typed a character: they want the provider's
    slower, fuller answer if it has one — a graph traversal, a semantic search. A provider
    with nothing slower simply ignores it."""


@dataclass(slots=True)
class SearchResult:
    """One hit. ``path`` is project-relative and opens the file in the Explorer."""

    title: str
    subtitle: str | None = None
    path: str | None = None
    line: int | None = None
    snippet: str | None = None
    kind: str | None = None
    """A short word for what this is — ``function``, ``class``, ``note`` — shown as a tag."""

    score: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "path": self.path,
            "line": self.line,
            "snippet": self.snippet,
            "kind": self.kind,
            "score": self.score,
        }


@dataclass(slots=True)
class SearchResponse:
    """A provider's whole answer: the hits, and what only a person should read."""

    results: list[SearchResult] = field(default_factory=list)
    answer: str | None = None
    """Prose or a report for the *deep* form of the query, shown under the hits."""

    note: str | None = None
    """One line about the source — how fresh it is, or why there is nothing yet."""

    def to_json(self) -> dict[str, Any]:
        return {
            "results": [result.to_json() for result in self.results],
            "answer": self.answer,
            "note": self.note,
        }


#: A hook run when the Studio opens the project this plugin is enabled in (§9.7).
ProjectOpenedHook = Callable[[PluginHost], Awaitable[None]]
#: A hook run for every event of the type it was registered for.
EventHook = Callable[[PluginHost, Event], Awaitable[None]]
#: ``POST /api/plugins/{id}/actions/{action}``: the JSON body in, a JSON-able value out.
ActionHandler = Callable[[PluginHost, dict[str, Any]], Awaitable[Any]]
#: An action on the Automations page (§6.5): what set the automation off in, a JSON-able
#: value or a sentence for its log out — once the work is done.
AutomationActionHandler = Callable[[PluginHost, dict[str, Any]], Awaitable[Any]]
#: The rail's search field, fanned out to every provider.
SearchHandler = Callable[[PluginHost, SearchQuery], Awaitable["SearchResponse | list[SearchResult]"]]


class NodeContext:
    """Everything an in-process (``python_function``) node gets to work with."""

    def __init__(
        self,
        *,
        inputs: dict[str, Any],
        params: dict[str, Any],
        project_root: Path,
        assets_dir: Path,
        work_dir: Path,
        settings: dict[str, Any],
        log_fn: Any,
        progress_fn: Any = None,
    ) -> None:
        self.inputs = inputs
        self.params = params
        self.project_root = project_root
        self.assets_dir = assets_dir
        self.work_dir = work_dir
        self.settings = settings
        self._log_fn = log_fn
        self._progress_fn = progress_fn

    def get(self, key: str, default: Any = None) -> Any:
        """Resolved value for a port: edge-supplied input wins over a literal param."""
        if key in self.inputs and self.inputs[key] is not None:
            return self.inputs[key]
        return self.params.get(key, default)

    async def log(self, message: str, *, stream: str = "stdout") -> None:
        await self._log_fn(message, stream)

    async def progress(
        self,
        *,
        percent: float | None = None,
        label: str | None = None,
        detail: str | None = None,
        eta_s: float | None = None,
        step: int | None = None,
        steps: int | None = None,
    ) -> None:
        """Say how far along this node is; the canvas draws it on the node.

        Advisory and unpersisted — it is how a long node stops looking hung, not a result.
        ``percent`` is 0–100 overall, ``label`` names the stage the node is in, and
        ``step``/``steps`` count repetitions within it. Everything is optional: a node
        that knows only which stage it reached passes only ``label``.
        """
        if self._progress_fn is None:
            return
        await self._progress_fn(
            {
                "percent": percent,
                "label": label,
                "detail": detail,
                "eta_s": eta_s,
                "step": step,
                "steps": steps,
            }
        )


class BaseNode:
    """Base class for node types.

    Subclasses declare ``schema``. Nodes whose ``execution.kind`` is ``python_function``
    also implement :meth:`run`; ``python_script`` and ``http_api`` nodes are driven
    entirely by the engine and need no Python body.
    """

    schema: ClassVar[NodeTypeSchema]

    async def run(self, ctx: NodeContext) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError(
            f"{type(self).__name__} declares kind='python_function' but does not implement run()"
        )


class PluginRegistry:
    """The handle passed to a plugin's ``register()`` function.

    Scoped to one plugin: every node type and page it adds is attributed to that
    plugin automatically, and node type ids are checked for the right namespace.
    """

    def __init__(self, registry: "Registry", plugin_id: str, settings: dict[str, Any]) -> None:
        self._registry = registry
        self.plugin_id = plugin_id
        self.settings = settings

    def add_node_type(self, node: type[BaseNode] | NodeTypeSchema) -> None:
        if isinstance(node, NodeTypeSchema):
            schema, node_cls = node, None
        else:
            schema = getattr(node, "schema", None)  # type: ignore[assignment]
            if not isinstance(schema, NodeTypeSchema):
                raise TypeError(
                    f"{node!r} must define a `schema` attribute of type NodeTypeSchema"
                )
            node_cls = node
        namespace = schema.id.split(".", 1)[0]
        if namespace != self.plugin_id:
            raise ValueError(
                f"Plugin {self.plugin_id!r} may only register node types namespaced "
                f"'{self.plugin_id}.*', got {schema.id!r}"
            )
        schema = schema.model_copy(update={"plugin_id": self.plugin_id})
        self._registry.register_node_type(schema, node_cls)
        log.debug("Registered node type %s", schema.id)

    def add_page(
        self,
        *,
        id: str,
        title: str,
        route: str,
        icon: str | None = None,
        component: str | None = None,
    ) -> None:
        self._registry.register_page(
            plugin_id=self.plugin_id,
            id=id,
            title=title,
            route=route,
            icon=icon,
            component=component,
        )

    def add_file_previewer(
        self,
        *,
        id: str,
        extensions: list[str],
        component: str,
        label: str | None = None,
        priority: int = 0,
    ) -> None:
        """Claim a file type in the Explorer preview pane (§6.2).

        ``component`` is a path inside the plugin to an ES module, loaded the same way
        as a plugin page. Declaring the same thing under ``frontend.previewers`` in
        ``plugin.yaml`` needs no Python at all; this is for previewers a plugin decides
        on at load time.
        """
        self._registry.register_previewer(
            plugin_id=self.plugin_id,
            id=id,
            extensions=[
                suffix if suffix.startswith(".") else f".{suffix}"
                for suffix in (raw.strip().lower() for raw in extensions)
                if suffix
            ],
            component=component,
            label=label,
            priority=priority,
        )

    def add_syntax_grammar(
        self,
        *,
        id: str,
        name: str,
        extensions: list[str],
        scope_name: str,
        grammar: str,
        aliases: list[str] | None = None,
    ) -> None:
        """Teach the Explorer to highlight a language (§6.2).

        ``grammar`` is a path inside the plugin to a **TextMate grammar** — the format
        VS Code uses — so an existing ``.tmLanguage.json`` works unmodified and no
        highlighting code is written at all. ``scope_name`` must be the ``scopeName``
        the file itself declares, or nothing will resolve it.

        Declaring the same thing under ``frontend.grammars`` in ``plugin.yaml`` needs no
        Python; this is for grammars a plugin decides on at load time.
        """
        self._registry.register_grammar(
            plugin_id=self.plugin_id,
            id=id,
            name=name,
            extensions=[
                suffix if suffix.startswith(".") else f".{suffix}"
                for suffix in (raw.strip().lower() for raw in extensions)
                if suffix
            ],
            scope_name=scope_name,
            grammar=grammar,
            aliases=list(aliases or []),
        )


    def on_project_opened(self, handler: ProjectOpenedHook, *, id: str | None = None) -> None:
        """Run ``handler(host)`` when the Studio opens a project this plugin is enabled in.

        That is server start-up, opening another project from the UI, and the moment the
        plugin is enabled in a project that was already open — each once. It runs in the
        background, after the project is up: a hook that takes minutes delays nothing and a
        hook that raises is logged, never fatal. It is cancelled if the project is closed
        first, so a subprocess it started should be killed on ``CancelledError``.
        """
        self._register_hook(handler, event=None, id=id)

    def on_event(self, event_type: EventType | str, handler: EventHook, *, id: str | None = None) -> None:
        """Run ``handler(host, event)`` for every event of that type on the Studio's bus.

        The types are those in :class:`~voxarium_studio.ws.events.EventType` — a task changing
        status, a run finishing, a file changing. Each call is its own background task, so
        a handler that must not overlap itself keeps a lock in ``host.state``.
        """
        self._register_hook(handler, event=str(event_type), id=id)

    def _register_hook(self, handler: Any, *, event: str | None, id: str | None) -> None:
        if not callable(handler):
            raise TypeError(f"{handler!r} is not callable")
        hook_id = id or f"{event or 'project.opened'}:{getattr(handler, '__name__', 'hook')}"
        self._registry.register_hook(
            plugin_id=self.plugin_id, id=hook_id, event=event, handler=handler
        )

    def add_action(self, id: str, handler: ActionHandler) -> None:
        """Expose ``handler(host, payload)`` as ``POST /api/plugins/{plugin}/actions/{id}``.

        This is how a plugin's settings page reaches its own backend: the runtime hands the
        page ``action(id, payload)``, bound to the plugin. The body arrives as ``payload``
        and whatever the handler returns is the response; raise ``ValueError`` for a bad
        request and the client gets a 400 with the message.
        """
        if not id.strip():
            raise ValueError("An action needs an id")
        if not callable(handler):
            raise TypeError(f"{handler!r} is not callable")
        self._registry.register_action(plugin_id=self.plugin_id, id=id.strip(), handler=handler)

    def add_search_provider(
        self, id: str, handler: SearchHandler, *, label: str | None = None
    ) -> None:
        """Answer the search field in the rail (§6.1).

        The field appears only once some plugin has registered a provider, and every
        keystroke reaches each of them as a :class:`SearchQuery`. Answer with a
        :class:`SearchResponse` — or just a list of :class:`SearchResult` — and answer
        quickly: the user is typing. Reserve anything slow for ``query.deep``.
        """
        if not id.strip():
            raise ValueError("A search provider needs an id")
        if not callable(handler):
            raise TypeError(f"{handler!r} is not callable")
        self._registry.register_search_provider(
            plugin_id=self.plugin_id, id=id.strip(), handler=handler, label=label
        )

    def add_automation_action(
        self,
        id: str,
        handler: AutomationActionHandler,
        *,
        label: str,
        description: str | None = None,
    ) -> None:
        """Offer ``handler(host, trigger)`` as an action on the Automations page (§6.5).

        ``label`` is what the page's picker calls it — "Graphify Update". ``trigger`` says
        what set the automation off: ``automation`` (its id and name), ``trigger`` (the kind,
        e.g. ``task.status_changed``) and ``values`` (the task's id and status, the changed
        paths, the run).

        **Return when the work is done, not when it has started.** The handler runs in the
        firing's own task, unattended, and may take as long as its work does: the log shows
        the firing *in progress* until it returns — a Studio page saying "done" about an index
        still being built is the thing this contract exists to prevent. What it returns (a
        sentence, or something JSON-able) is the log's line; raise to record the firing as
        failed, or :class:`AutomationCancelled` for work that was stopped. Work the plugin
        runs for itself (``host.spawn()``) is waited on rather than returned from.

        Separate from :meth:`add_action` on purpose: a settings page's endpoints are not all
        safe to run on a trigger nobody is watching, so nothing is offered until a plugin
        says so here.
        """
        if not id.strip():
            raise ValueError("An automation action needs an id")
        if not label.strip():
            raise ValueError("An automation action needs a label")
        if not callable(handler):
            raise TypeError(f"{handler!r} is not callable")
        self._registry.register_automation_action(
            plugin_id=self.plugin_id,
            id=id.strip(),
            handler=handler,
            label=label.strip(),
            description=description,
        )

    @property
    def plugin_root(self) -> Path:
        return self._registry.plugin_root(self.plugin_id)

    @property
    def project_root(self) -> Path:
        return self._registry.project_root


__all__ = [
    "ActionHandler",
    "AutomationActionHandler",
    "AutomationCancelled",
    "BaseNode",
    "Event",
    "EventHook",
    "ExecutionSpec",
    "NodeContext",
    "NodeTypeSchema",
    "PluginHost",
    "PluginRegistry",
    "PortSchema",
    "ProjectOpenedHook",
    "SearchHandler",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
]

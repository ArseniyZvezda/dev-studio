"""Plugin discovery and loading (§9.2, §9.4).

Search order, later entries winning on id collision:

1. ``plugins_builtin/`` shipped with the Studio
2. the machine-level plugin directory (installed once, available to every project)
3. ``<managed-project>/voxarium_studio/plugins/`` for project-local plugins and overrides

Trust model: plugin code executes unsandboxed in the backend process. This is the
explicit v1 assumption from §9.4 — a plugin is something the user installs knowingly,
as with Blender addons or VS Code extensions.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from pathlib import Path

from ..core.config import AppConfig
from ..core.paths import ProjectPaths, user_config_dir
from .api import PluginRegistry
from .manifest import MANIFEST_FILENAME, PluginManifest
from .registry import LoadedPlugin, Registry

log = logging.getLogger(__name__)


def builtin_plugins_dir() -> Path | None:
    """``plugins_builtin`` sits next to the package when installed, one level up in a source tree."""
    package_dir = Path(__file__).resolve().parent.parent
    for candidate in (package_dir / "plugins_builtin", package_dir.parent / "plugins_builtin"):
        if candidate.is_dir():
            return candidate
    return None


def global_plugins_dir() -> Path:
    return user_config_dir() / "plugins"


def discover_manifests(paths: ProjectPaths) -> list[PluginManifest]:
    """Scan all three locations; later scopes override earlier ones by plugin id."""
    found: dict[str, PluginManifest] = {}
    sources: list[tuple[Path | None, str]] = [
        (builtin_plugins_dir(), "builtin"),
        (global_plugins_dir(), "global"),
        (paths.plugins, "project"),
    ]
    for directory, scope in sources:
        if directory is None or not directory.is_dir():
            continue
        for manifest_path in sorted(directory.glob(f"*/{MANIFEST_FILENAME}")):
            try:
                manifest = PluginManifest.from_file(manifest_path, scope=scope)
            except Exception as exc:
                log.error("Invalid plugin manifest %s: %s", manifest_path, exc)
                continue
            if manifest.id == "plastic":
                continue  # migrated to the independent VCS catalog (§9.6)
            if manifest.id in found:
                log.info(
                    "Plugin %r from %s overrides the %s copy",
                    manifest.id,
                    scope,
                    found[manifest.id].scope,
                )
            found[manifest.id] = manifest
    return list(found.values())


def _is_inside(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:  # pragma: no cover - defensive
        return False
    return resolved == root or root in resolved.parents


def _forget_plugin_modules(manifest: PluginManifest, before: set[str], keep: str) -> None:
    """Drop the plugin's own modules from ``sys.modules`` once it has finished loading.

    A sibling imported from the plugin root arrives under its **bare** name — `helpers`,
    not `voxarium_studio_plugin_x.helpers` — because that is how the path entry resolves it, and
    `sys.modules` is process-global. Left there it causes two bugs: a second plugin
    shipping its own `helpers.py` silently gets the first one, and reloading a plugin
    re-runs `register()` against the copy imported the first time, so an edited sibling
    never takes effect.

    Only names this load actually added are removed, so a module the host or a test had
    already imported is left alone. Objects the entry module holds stay alive by
    reference — this releases the global name, not the code.
    """
    assert manifest.root is not None
    root = manifest.root.resolve()
    for name in sorted(set(sys.modules) - before):
        if name == keep:
            continue  # the entry module is already namespaced by plugin id
        module = sys.modules.get(name)
        origin = getattr(module, "__file__", None)
        locations = [origin] if origin else list(getattr(module, "__path__", None) or [])
        if locations and all(_is_inside(Path(location), root) for location in locations):
            del sys.modules[name]


def _import_entry_module(manifest: PluginManifest, module_file: str):
    """Import the plugin's entry module under a unique name.

    The plugin root goes on ``sys.path`` for the duration so intra-plugin imports
    (``from helpers import ...`` style siblings) resolve. Both the path entry and the
    modules it produced are withdrawn afterwards, so a sibling import has to happen while
    the entry module is being executed — not lazily from inside a function.
    """
    assert manifest.root is not None
    module_path = (manifest.root / module_file).resolve()
    if not module_path.exists():
        raise FileNotFoundError(f"Plugin entry module not found: {module_path}")
    if not manifest.root == module_path.parent and manifest.root not in module_path.parents:
        raise ValueError(f"Plugin entry {module_path} escapes plugin root {manifest.root}")

    module_name = f"voxarium_studio_plugin_{manifest.id}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not build an import spec for {module_path}")
    module = importlib.util.module_from_spec(spec)

    root_str = str(manifest.root)
    added = root_str not in sys.path
    if added:
        sys.path.insert(0, root_str)
    before = set(sys.modules)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if added:
            try:
                sys.path.remove(root_str)
            except ValueError:  # pragma: no cover - defensive
                pass
        _forget_plugin_modules(manifest, before, keep=module_name)
    return module


def load_plugin(
    registry: Registry, manifest: PluginManifest, *, config: AppConfig
) -> LoadedPlugin:
    """Load one plugin into the registry, recording failures rather than raising.

    A broken third-party plugin must not stop the Studio from starting, so the error
    is captured on the ``LoadedPlugin`` and surfaced in the UI.
    """
    enabled = config.is_plugin_enabled(manifest.id, default=manifest.enabled_by_default)
    plugin = LoadedPlugin(manifest=manifest, enabled=enabled)
    registry.add_plugin(plugin)

    if not enabled:
        plugin.status = "disabled"
        return plugin

    settings = config.plugins.get(manifest.id)
    plugin_registry = PluginRegistry(
        registry, manifest.id, settings.settings if settings else {}
    )

    try:
        if manifest.backend and manifest.backend.entry:
            module_file, func_name = manifest.backend.split_entry()
            module = _import_entry_module(manifest, module_file)
            register_fn = getattr(module, func_name, None)
            if register_fn is None:
                raise AttributeError(
                    f"{module_file} has no attribute {func_name!r} "
                    f"(declared as backend.entry)"
                )
            register_fn(plugin_registry)

        # Pages, previewers and grammars declared purely in the manifest need no backend code.
        if manifest.frontend:
            for page in manifest.frontend.pages:
                plugin_registry.add_page(
                    id=page.id,
                    title=page.title,
                    route=page.route,
                    icon=page.icon,
                    component=page.component,
                )
            for previewer in manifest.frontend.previewers:
                plugin_registry.add_file_previewer(
                    id=previewer.id,
                    extensions=previewer.extensions,
                    component=previewer.component,
                    label=previewer.label,
                    priority=previewer.priority,
                )
            for grammar in manifest.frontend.grammars:
                plugin_registry.add_syntax_grammar(
                    id=grammar.id,
                    name=grammar.name,
                    extensions=grammar.extensions,
                    scope_name=grammar.scope_name,
                    grammar=grammar.grammar,
                    aliases=grammar.aliases,
                )
            component_by_type = {n.node_type: n.component for n in manifest.frontend.nodes}
            for node_type_id, component in component_by_type.items():
                schema = registry.node_type(node_type_id)
                if schema is None:
                    log.warning(
                        "Plugin %s declares a UI component for unknown node type %s",
                        manifest.id,
                        node_type_id,
                    )
                    continue
                registry.register_node_type(
                    schema.model_copy(update={"ui_component": component}),
                    registry.node_class(node_type_id),
                )
    except Exception as exc:
        plugin.status = "error"
        plugin.error = f"{type(exc).__name__}: {exc}"
        log.error("Failed to load plugin %s: %s", manifest.id, exc)
        log.debug("%s", traceback.format_exc())
        return plugin

    plugin.status = "loaded"
    return plugin


def load_all(paths: ProjectPaths, config: AppConfig) -> Registry:
    """Build a fresh registry from disk. Also used to hot-reload after enable/disable."""
    from ..vcs.nodes import register_nodes

    registry = Registry(project_root=paths.root)
    register_nodes(registry)
    for manifest in discover_manifests(paths):
        load_plugin(registry, manifest, config=config)
    loaded = [p for p in registry.plugins() if p.status == "loaded"]
    log.info(
        "Loaded %d plugin(s), %d node type(s)", len(loaded), len(registry.node_types())
    )
    return registry

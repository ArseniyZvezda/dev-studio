"""Plugin discovery, the registry, and the extension points a plugin contributes (§9)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from voxarium_studio.core.config import AppConfig, PluginConfig
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.plugins import loader as plugin_loader
from voxarium_studio.plugins.manifest import PluginManifest
from voxarium_studio.plugins.registry import Registry, plugin_asset_url


def write_plugin(directory: Path, manifest: dict, files: dict[str, str] | None = None) -> Path:
    root = directory / manifest["id"]
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    for name, body in (files or {}).items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return root


# ------------------------------------------------------------------ manifest


def test_manifest_rejects_a_bad_id(tmp_path: Path) -> None:
    path = write_plugin(tmp_path, {"id": "ok", "name": "n"}) / "plugin.yaml"
    path.write_text(yaml.safe_dump({"id": "not/valid", "name": "n"}), encoding="utf-8")
    with pytest.raises(ValueError, match="alphanumeric"):
        PluginManifest.from_file(path)


def test_manifest_rejects_a_backend_entry_without_a_function(tmp_path: Path) -> None:
    path = write_plugin(tmp_path, {"id": "p", "name": "n"}) / "plugin.yaml"
    path.write_text(
        yaml.safe_dump({"id": "p", "name": "n", "backend": {"entry": "plugin.py"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="<file.py>:<func>"):
        PluginManifest.from_file(path)


def test_previewer_extensions_are_normalised(tmp_path: Path) -> None:
    path = write_plugin(
        tmp_path,
        {
            "id": "p",
            "name": "n",
            "frontend": {
                "previewers": [
                    {"id": "v", "extensions": ["VOX", " .Fbx "], "component": "f/V.js"}
                ]
            },
        },
    ) / "plugin.yaml"
    manifest = PluginManifest.from_file(path)
    assert manifest.frontend.previewers[0].extensions == [".vox", ".fbx"]


def test_a_previewer_must_claim_something(tmp_path: Path) -> None:
    path = write_plugin(
        tmp_path,
        {"id": "p", "name": "n",
         "frontend": {"previewers": [{"id": "v", "extensions": [], "component": "f/V.js"}]}},
    ) / "plugin.yaml"
    with pytest.raises(ValueError, match="at least one file extension"):
        PluginManifest.from_file(path)


def grammar_manifest(**overrides) -> dict:
    """A manifest declaring one syntax grammar, for the parse-level checks."""
    grammar = {
        "id": "toy",
        "name": "Toy",
        "extensions": ["TOY", " .Tyy ", ""],
        "scope_name": "source.toy",
        "grammar": "frontend/toy.tmLanguage.json",
        "aliases": ["t"],
    }
    grammar.update(overrides)
    return {"id": "p", "name": "n", "frontend": {"grammars": [grammar]}}


def test_grammar_extensions_are_normalised(tmp_path: Path) -> None:
    """Same rule as a previewer's: lower-cased, dot-prefixed, blanks dropped."""
    path = write_plugin(tmp_path, grammar_manifest()) / "plugin.yaml"
    grammar = PluginManifest.from_file(path).frontend.grammars[0]

    assert grammar.extensions == [".toy", ".tyy"]
    assert grammar.scope_name == "source.toy"
    assert grammar.aliases == ["t"]


def test_a_grammar_must_claim_something(tmp_path: Path) -> None:
    path = write_plugin(tmp_path, grammar_manifest(extensions=[])) / "plugin.yaml"
    with pytest.raises(ValueError, match="at least one file extension"):
        PluginManifest.from_file(path)


def test_a_grammar_must_declare_a_scope_name(tmp_path: Path) -> None:
    """A highlighter resolves a grammar by scope; a blank one can never be selected."""
    path = write_plugin(tmp_path, grammar_manifest(scope_name="  ")) / "plugin.yaml"
    with pytest.raises(ValueError, match="scope_name"):
        PluginManifest.from_file(path)


# ------------------------------------------------------------------ built-ins


def test_the_builtin_plugins_load(registry: Registry) -> None:
    loaded = {plugin.id for plugin in registry.plugins() if plugin.status == "loaded"}
    assert {"core", "hunyuan3d", "vox_preview", "fbx_preview", "angelscript"} <= loaded

    broken = [(p.id, p.error) for p in registry.plugins() if p.status == "error"]
    assert broken == []


def test_node_type_ids_are_namespaced_by_their_plugin(registry: Registry) -> None:
    for schema in registry.node_types():
        namespace = schema.id.split(".", 1)[0]
        if schema.plugin_id.startswith("vcs:"):
            assert namespace == schema.plugin_id.removeprefix("vcs:")
            assert registry.plugin(schema.plugin_id) is None
        else:
            assert namespace == schema.plugin_id


def test_all_three_execution_kinds_have_a_built_in_example(registry: Registry) -> None:
    """`http_api` exists because hosted generation backends are the common case (§8.1)."""
    kinds = {schema.execution.kind for schema in registry.node_types()}
    assert kinds == {"python_function", "python_script", "http_api"}


def test_previewers_are_registered_for_the_3d_formats(registry: Registry) -> None:
    assert registry.previewer_for(".vox").plugin_id == "vox_preview"
    assert registry.previewer_for(".fbx").plugin_id == "fbx_preview"
    # The mesh formats Hunyuan3D actually exports, claimed by one plugin between them.
    assert registry.previewer_for(".glb").plugin_id == "mesh_preview"
    assert registry.previewer_for(".stl").plugin_id == "mesh_preview"
    assert registry.previewer_for(".VOX") is not None, "matching is case-insensitive"
    assert registry.previewer_for("vox") is not None, "a missing dot is tolerated"
    # `.ply` is a 3D suffix the Explorer knows and no plugin claims, which is what keeps
    # this checking the fallback rather than the list of installed previewers.
    assert registry.previewer_for(".ply") is None, "unclaimed types fall back to the panel"


# ------------------------------------------------------------------ syntax grammars


def test_the_angelscript_plugin_is_the_first_grammar(registry: Registry) -> None:
    """A plugin teaches the Studio a language; `angelscript` is the worked example (§6.2)."""
    grammars = {grammar.id: grammar for grammar in registry.grammars()}

    grammar = grammars["angelscript"]
    assert grammar.plugin_id == "angelscript"
    assert grammar.name == "AngelScript"
    assert grammar.extensions == (".as",)
    assert grammar.scope_name == "source.angelscript"
    assert set(grammar.aliases) == {"as", "angelscript"}
    assert registry.plugin("angelscript").to_json()["grammars"] == ["angelscript"]


def walk_patterns(rules, where: str):
    """Yield every rule in a TextMate `patterns` list, descending into nested ones."""
    for index, rule in enumerate(rules):
        location = f"{where}[{index}]"
        yield location, rule
        yield from walk_patterns(rule.get("patterns", []), f"{location}.patterns")


def test_the_shipped_angelscript_grammar_is_a_well_formed_textmate_grammar(
    registry: Registry,
) -> None:
    """The grammar file *is* this plugin's whole output, so check the file itself.

    It is a standard TextMate grammar — the format VS Code uses — which is what lets an
    existing `.tmLanguage.json` be dropped in with no code at all.
    """
    plugin = registry.plugin("angelscript")
    declared = plugin.manifest.frontend.grammars[0]
    grammar = json.loads((plugin.manifest.root / declared.grammar).read_text(encoding="utf-8"))

    assert grammar["scopeName"] == declared.scope_name, "the manifest names the file's own scope"
    assert grammar["patterns"], "a grammar with no top-level patterns highlights nothing"

    rules = list(walk_patterns(grammar["patterns"], "patterns"))
    for name, entry in grammar["repository"].items():
        rules.append((f"repository.{name}", entry))
        rules.extend(walk_patterns(entry.get("patterns", []), f"repository.{name}.patterns"))

    for location, rule in rules:
        assert (
            "match" in rule or "begin" in rule or "include" in rule or "patterns" in rule
        ), f"{location} matches nothing and includes nothing"
        if "begin" in rule:
            assert "end" in rule or "while" in rule, f"{location} begins without ending"

    scopes = {rule["name"] for _, rule in rules if "name" in rule}
    assert {
        "comment.line.double-slash.angelscript",
        "comment.block.angelscript",
        "string.quoted.double.angelscript",
        "string.quoted.single.angelscript",
        "string.quoted.triple.angelscript",
        "keyword.control.angelscript",
        "storage.type.angelscript",
        "storage.modifier.angelscript",
        "constant.language.angelscript",
        "meta.preprocessor.angelscript",
        "meta.metadata.angelscript",
    } <= scopes, "conventional scope names are what a theme has to colour"


def test_grammars_are_served_with_a_version_stamped_url(client: TestClient) -> None:
    """`grammar_url` goes through `asset_url`, so it changes when the grammar file does."""
    payload = client.get("/api/plugins/grammars").json()
    grammar = next(entry for entry in payload if entry["id"] == "angelscript")

    assert grammar == {
        "id": "angelscript",
        "plugin_id": "angelscript",
        "name": "AngelScript",
        "extensions": [".as"],
        "scope_name": "source.angelscript",
        "aliases": ["as", "angelscript"],
        "grammar_url": grammar["grammar_url"],
    }
    url, _, version = grammar["grammar_url"].partition("?v=")
    assert url == "/api/plugins/angelscript/assets/frontend/angelscript.tmLanguage.json"
    assert version, "an unstamped URL is one a browser may answer from its own cache"

    # `.json` has to be servable by the asset endpoint, or the stamped URL 403s.
    served = client.get(grammar["grammar_url"])
    assert served.status_code == 200
    assert served.json()["scopeName"] == "source.angelscript"


def test_disabling_the_plugin_withdraws_its_grammar(client: TestClient) -> None:
    """A grammar lives on the registry so a disable actually takes it away — the same
    reason version control backends do."""

    def grammar_ids() -> list[str]:
        return [entry["id"] for entry in client.get("/api/plugins/grammars").json()]

    assert "angelscript" in grammar_ids()

    disabled = client.post("/api/plugins/angelscript/disable")
    assert disabled.status_code == 200
    assert disabled.json()["grammars"] == []
    assert "angelscript" not in grammar_ids()

    enabled = client.post("/api/plugins/angelscript/enable")
    assert enabled.status_code == 200
    assert enabled.json()["grammars"] == ["angelscript"]
    assert "angelscript" in grammar_ids()


def test_two_plugins_may_claim_the_same_extension(project: Path) -> None:
    """Precedence is the frontend's decision; the registry only has to not lose one."""
    paths = ProjectPaths.for_project(project)
    write_plugin(
        paths.plugins,
        {
            "id": "angelscript_alt",
            "name": "AngelScript (alternative)",
            "frontend": {
                "grammars": [
                    {
                        "id": "angelscript-alt",
                        "name": "AngelScript",
                        "extensions": [".as"],
                        "scope_name": "source.angelscript.alt",
                        "grammar": "frontend/alt.tmLanguage.json",
                    }
                ]
            },
        },
        {"frontend/alt.tmLanguage.json": '{"scopeName": "source.angelscript.alt"}'},
    )
    registry = plugin_loader.load_all(paths, AppConfig())

    claiming = [g.id for g in registry.grammars() if ".as" in g.extensions]
    assert claiming == ["angelscript", "angelscript-alt"]


def test_a_plugin_can_add_a_grammar_from_python(project: Path) -> None:
    """`frontend.grammars` needs no code; `add_syntax_grammar` is for load-time decisions."""
    paths = ProjectPaths.for_project(project)
    write_plugin(
        paths.plugins,
        {"id": "lang", "name": "Lang", "backend": {"entry": "plugin.py:register"}},
        {
            "plugin.py": (
                "def register(registry):\n"
                "    registry.add_syntax_grammar(\n"
                "        id='toy', name='Toy', extensions=['TOY', ' .tyy '],\n"
                "        scope_name='source.toy', grammar='frontend/toy.tmLanguage.json',\n"
                "        aliases=['t'])\n"
            )
        },
    )
    registry = plugin_loader.load_all(paths, AppConfig())

    grammar = next(g for g in registry.grammars() if g.id == "toy")
    assert grammar.plugin_id == "lang"
    assert grammar.extensions == (".toy", ".tyy"), "normalised as the manifest path is"
    assert registry.plugin("lang").grammar_ids == ["toy"]

    registry.remove_plugin("lang")
    assert [g.id for g in registry.grammars() if g.id == "toy"] == []


def test_plugin_frontend_modules_exist_where_the_manifest_says(registry: Registry) -> None:
    """A declared component that 404s renders as a broken page, so check the files ship."""
    for plugin in registry.plugins():
        frontend = plugin.manifest.frontend
        if frontend is None or plugin.manifest.root is None:
            continue
        declared = (
            [page.component for page in frontend.pages]
            + [node.component for node in frontend.nodes]
            + [previewer.component for previewer in frontend.previewers]
            + [grammar.grammar for grammar in frontend.grammars]
        )
        for component in filter(None, declared):
            assert (plugin.manifest.root / component).is_file(), f"{plugin.id}: {component}"


def test_asset_urls_are_built_in_one_place() -> None:
    assert plugin_asset_url("p", "frontend/A.js") == "/api/plugins/p/assets/frontend/A.js"


def test_asset_urls_carry_a_version_that_moves_with_the_file(tmp_path: Path) -> None:
    """The stamp is the only thing that reliably keeps a browser off a stale module.

    `Cache-Control: no-cache` asks for revalidation, but a browser holding a copy it
    considers fresh never asks — so the URL has to stop being the same URL.
    """
    asset = tmp_path / "frontend" / "A.js"
    asset.parent.mkdir()
    asset.write_text("one", encoding="utf-8")

    first = plugin_asset_url("p", "frontend/A.js", tmp_path)
    assert first.startswith("/api/plugins/p/assets/frontend/A.js?v=")
    assert plugin_asset_url("p", "frontend/A.js", tmp_path) == first, "stable while unedited"

    # A different size is enough; mtime alone has a coarse resolution on some filesystems.
    asset.write_text("one and a bit more", encoding="utf-8")
    assert plugin_asset_url("p", "frontend/A.js", tmp_path) != first

    # A missing file is the endpoint's 404 to report; the URL builder must not raise.
    assert plugin_asset_url("p", "frontend/gone.js", tmp_path).endswith("gone.js")


# ------------------------------------------------------------------ registration


def test_disabled_plugins_contribute_nothing(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    config = AppConfig()
    config.plugins["vox_preview"] = PluginConfig(enabled=False)
    registry = plugin_loader.load_all(paths, config)

    assert registry.plugin("vox_preview").status == "disabled"
    assert registry.previewer_for(".vox") is None
    assert registry.previewer_for(".fbx") is not None, "other plugins are unaffected"


def test_a_broken_plugin_is_recorded_rather_than_stopping_start_up(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    write_plugin(
        paths.plugins,
        {"id": "broken", "name": "Broken", "backend": {"entry": "plugin.py:register"}},
        {"plugin.py": "raise RuntimeError('boom')\n"},
    )
    registry = plugin_loader.load_all(paths, AppConfig())

    plugin = registry.plugin("broken")
    assert plugin.status == "error"
    assert "boom" in plugin.error
    assert registry.plugin("core").status == "loaded", "the rest still loads"


def test_a_project_local_plugin_overrides_a_builtin(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    write_plugin(paths.plugins, {"id": "vox_preview", "name": "My VOX", "version": "9.9.9"})
    registry = plugin_loader.load_all(paths, AppConfig())

    plugin = registry.plugin("vox_preview")
    assert plugin.manifest.scope == "project"
    assert plugin.manifest.version == "9.9.9"


def test_a_plugin_may_not_namespace_node_types_under_another_plugin(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    write_plugin(
        paths.plugins,
        {"id": "squatter", "name": "Squatter", "backend": {"entry": "plugin.py:register"}},
        {
            "plugin.py": (
                "from voxarium_studio.plugin_api import ExecutionSpec, NodeTypeSchema\n"
                "def register(registry):\n"
                "    registry.add_node_type(NodeTypeSchema(\n"
                "        id='core.hijacked', name='x', category='x',\n"
                "        execution=ExecutionSpec(kind='python_function', entry='')))\n"
            )
        },
    )
    registry = plugin_loader.load_all(paths, AppConfig())

    assert registry.plugin("squatter").status == "error"
    assert "may only register node types namespaced" in registry.plugin("squatter").error
    assert registry.node_type("core.hijacked") is None


# ------------------------------------------------------------------ module isolation


def write_plugin_with_sibling(paths: ProjectPaths, plugin_id: str, marker: str) -> None:
    """A plugin whose entry module imports a sibling — both called `helpers` on purpose."""
    write_plugin(
        paths.plugins,
        {"id": plugin_id, "name": plugin_id, "backend": {"entry": "plugin.py:register"}},
        {
            "helpers.py": f"MARKER = {marker!r}\n",
            "plugin.py": (
                "from helpers import MARKER\n"
                "from voxarium_studio.plugin_api import ExecutionSpec, NodeTypeSchema\n"
                "def register(registry):\n"
                "    registry.add_node_type(NodeTypeSchema(\n"
                f"        id='{plugin_id}.n', name=MARKER, category='x',\n"
                "        execution=ExecutionSpec(kind='python_function', entry='')))\n"
            ),
        },
    )


def test_two_plugins_may_ship_a_sibling_module_of_the_same_name(project: Path) -> None:
    """A sibling resolves under its bare name, so without cleanup the second plugin
    would silently import the first one's copy."""
    paths = ProjectPaths.for_project(project)
    write_plugin_with_sibling(paths, "alpha", "from-alpha")
    write_plugin_with_sibling(paths, "beta", "from-beta-plugin")

    registry = plugin_loader.load_all(paths, AppConfig())

    assert registry.node_type("alpha.n").name == "from-alpha"
    assert registry.node_type("beta.n").name == "from-beta-plugin"


def test_reloading_a_plugin_re_imports_its_siblings(project: Path) -> None:
    """Enable/disable and a settings save both re-run `register()`; an edited sibling
    has to come with it, or the plugin keeps running code that is no longer on disk."""
    paths = ProjectPaths.for_project(project)
    write_plugin_with_sibling(paths, "alpha", "before")

    assert plugin_loader.load_all(paths, AppConfig()).node_type("alpha.n").name == "before"
    (paths.plugins / "alpha" / "helpers.py").write_text("MARKER = 'after-edit'\n", encoding="utf-8")

    assert plugin_loader.load_all(paths, AppConfig()).node_type("alpha.n").name == "after-edit"


def test_a_plugins_modules_do_not_linger_under_a_bare_name(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    write_plugin_with_sibling(paths, "alpha", "x")

    plugin_loader.load_all(paths, AppConfig())

    assert "helpers" not in sys.modules
    # The entry module stays, but it was namespaced by plugin id to begin with.
    assert "voxarium_studio_plugin_alpha" in sys.modules


def test_a_module_the_host_already_imported_is_left_alone(project: Path, monkeypatch) -> None:
    """Cleanup is scoped to names this load added — anything already imported is not ours."""
    paths = ProjectPaths.for_project(project)
    write_plugin_with_sibling(paths, "alpha", "from-disk")

    preloaded = types.ModuleType("helpers")
    preloaded.MARKER = "from-the-host"
    monkeypatch.setitem(sys.modules, "helpers", preloaded)

    registry = plugin_loader.load_all(paths, AppConfig())

    assert registry.node_type("alpha.n").name == "from-the-host"
    assert sys.modules["helpers"] is preloaded


def test_plugin_settings_shape_what_a_plugin_registers(project: Path) -> None:
    """`PluginRegistry.settings` is how a plugin configures itself per project (§9.4)."""
    paths = ProjectPaths.for_project(project)
    config = AppConfig()
    config.plugins["hunyuan3d"] = PluginConfig(settings={"default_resolution": 96})
    registry = plugin_loader.load_all(paths, config)

    resolution = next(
        port for port in registry.node_type("hunyuan3d.voxelize").inputs if port.id == "resolution"
    )
    assert resolution.default == 96


def test_removing_a_plugin_drops_everything_it_contributed(registry: Registry) -> None:
    assert registry.node_type("hunyuan3d.voxelize") is not None
    registry.remove_plugin("hunyuan3d")
    assert registry.node_type("hunyuan3d.voxelize") is None
    assert registry.plugin("hunyuan3d") is None
    assert registry.node_type("core.echo") is not None

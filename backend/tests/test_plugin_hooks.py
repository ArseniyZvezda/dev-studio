"""The three extension points that are not nodes or pages: hooks, actions and search (§9.7).

A plugin written into the project's own plugin directory registers one of each, and the
tests watch what the context does with them — when a *project opened* hook runs and when
it does not, that an event on the bus reaches an event hook, that an action is an endpoint
and a provider answers the search field. The built-in `graphify` plugin, which is what
these exist for, has a file of its own.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest
import yaml
from fastapi.testclient import TestClient

from voxarium_studio.core.config import load_config
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.main import create_app
from voxarium_studio.plugins import loader as plugin_loader
from voxarium_studio.ws.events import EventType

HOOKED = dedent(
    '''
    """A plugin that uses every extension point in §9.7 and records what it was handed."""

    import json

    from voxarium_studio.plugin_api import PluginRegistry, SearchResponse, SearchResult


    async def opened(host):
        host.state["opened"] = host.state.get("opened", 0) + 1
        (host.data_dir / "opened.txt").write_text(str(host.state["opened"]), encoding="utf-8")
        await host.publish("opened", {"count": host.state["opened"]})


    async def broken(host):
        raise RuntimeError("hook blew up")


    async def on_status(host, event):
        seen = host.state.setdefault("statuses", [])
        seen.append(event.data.get("status"))
        (host.data_dir / "statuses.json").write_text(json.dumps(seen), encoding="utf-8")


    async def echo(host, payload):
        if payload.get("fail"):
            raise ValueError("bad payload")
        return {
            "echo": payload,
            "launch_root": str(host.launch_root),
            "settings": host.settings,
            "opened": host.state.get("opened", 0),
        }


    async def find(host, query):
        if query.text == "boom":
            raise RuntimeError("provider exploded")
        return SearchResponse(
            results=[SearchResult(title=f"hit for {query.text}", path="a.txt", line=3, kind="file")],
            note="deep" if query.deep else "quick",
        )


    def register(registry: PluginRegistry) -> None:
        registry.on_project_opened(opened)
        registry.on_project_opened(broken)
        registry.on_event("team.task.status_changed", on_status)
        registry.add_action("echo", echo)
        registry.add_search_provider("find", find, label="Finder")
    '''
)


@pytest.fixture
def hooked(project: Path) -> Path:
    """The sample project with the plugin above installed project-locally."""
    root = ProjectPaths.for_project(project).plugins / "hooked"
    root.mkdir(parents=True)
    manifest = {"id": "hooked", "name": "Hooked", "backend": {"entry": "plugin.py:register"}}
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (root / "plugin.py").write_text(HOOKED, encoding="utf-8")
    return project


async def eventually(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Hooks run in the background; wait for their effect rather than for them."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def drained(ctx: AppContext, timeout: float = 5.0) -> None:
    await eventually(lambda: not ctx.plugin_tasks(), timeout)


# ------------------------------------------------------------------ the registry


def test_hooks_actions_and_providers_are_the_plugins_and_go_with_it(hooked: Path) -> None:
    paths = ProjectPaths.for_project(hooked)
    registry = plugin_loader.load_all(paths, load_config(paths))

    plugin = registry.plugin("hooked")
    assert plugin.status == "loaded", plugin.error
    listed = plugin.to_json()
    assert listed["hooks"] == [
        "project.opened:opened",
        "project.opened:broken",
        "team.task.status_changed:on_status",
    ]
    assert listed["actions"] == ["echo"]
    assert listed["search_providers"] == ["find"]

    assert [hook.id for hook in registry.project_opened_hooks() if hook.plugin_id == "hooked"] == [
        "project.opened:opened",
        "project.opened:broken",
    ]
    assert [h.id for h in registry.event_hooks("team.task.status_changed") if h.plugin_id == "hooked"] == [
        "team.task.status_changed:on_status"
    ]
    assert registry.event_hooks("team.task.created") == [] or all(
        hook.plugin_id != "hooked" for hook in registry.event_hooks("team.task.created")
    )
    assert registry.action("hooked", "echo") is not None
    provider = next(p for p in registry.search_providers() if p.plugin_id == "hooked")
    assert (provider.key, provider.label) == ("hooked.find", "Finder")

    registry.remove_plugin("hooked")
    assert registry.action("hooked", "echo") is None
    assert all(p.plugin_id != "hooked" for p in registry.search_providers())
    assert all(h.plugin_id != "hooked" for h in registry.hooks())


# ------------------------------------------------------------------ the context


async def test_project_opened_hooks_run_once_per_opening(
    hooked: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Start-up is an opening; a reload after a settings save is not; enabling the plugin
    again is. And a hook that raises is the plugin's problem, logged, not the Studio's."""
    ctx = AppContext.create(str(hooked), AuthSettings())
    await ctx.startup()
    try:
        marker = ctx.paths.data / "opened.txt"
        await eventually(marker.exists)
        await drained(ctx)
        assert marker.read_text(encoding="utf-8") == "1"
        assert "hook blew up" in caplog.text, "the broken hook was logged"
        assert "plugin-hook:hooked:project.opened:broken" in caplog.text

        published = [e for e in ctx.bus.replay(0) if e.type == EventType.PLUGIN_EVENT]
        assert published[-1].data == {"plugin_id": "hooked", "name": "opened", "data": {"count": 1}}

        await ctx.reload_plugins()
        await drained(ctx)
        assert marker.read_text(encoding="utf-8") == "1", "a settings-save reload is not an opening"

        ctx.config.plugin_state("hooked").enabled = False
        await ctx.reload_plugins()
        await drained(ctx)
        assert ctx.registry.action("hooked", "echo") is None, "disabled means withdrawn"

        ctx.config.plugin_state("hooked").enabled = True
        await ctx.reload_plugins()
        await eventually(lambda: marker.read_text(encoding="utf-8") == "2")
        await drained(ctx)
        assert ctx.plugin_host("hooked").state["opened"] == 2, "state outlived both reloads"
    finally:
        await ctx.shutdown()


async def test_an_event_on_the_bus_reaches_the_hook_registered_for_it(hooked: Path) -> None:
    ctx = AppContext.create(str(hooked), AuthSettings())
    await ctx.startup()
    try:
        await ctx.bus.publish(EventType.TEAM_TASK_CREATED, {"task_id": "t"})
        await ctx.bus.publish(EventType.TEAM_TASK_STATUS_CHANGED, {"task_id": "t", "status": "done"})
        record = ctx.paths.data / "statuses.json"
        await eventually(record.exists)
        await drained(ctx)
        assert json.loads(record.read_text(encoding="utf-8")) == ["done"], (
            "only the subscribed type, and every one of it"
        )
    finally:
        await ctx.shutdown()


async def test_the_host_is_built_per_call_over_the_launch_root_of_the_moment(hooked: Path) -> None:
    ctx = AppContext.create(str(hooked), AuthSettings())
    await ctx.startup()
    try:
        await drained(ctx)
        host = ctx.plugin_host("hooked")
        assert host.project_root == ctx.paths.root
        assert host.data_dir == ctx.paths.data
        assert host.launch_root == ctx.paths.root
        assert host.settings == {}

        ctx.config.teams.launch_root = ".."
        assert host.launch_root == ctx.paths.root.parent, "read live, not captured"
        ctx.config.teams.launch_root = "no/such/dir"
        assert host.launch_root == ctx.paths.root, "a missing root falls back to the project"

        ctx.config.plugin_state("hooked").settings = {"depth": 3}
        assert host.settings == {"depth": 3}
    finally:
        await ctx.shutdown()


async def test_shutdown_cancels_what_a_plugin_left_running(hooked: Path) -> None:
    ctx = AppContext.create(str(hooked), AuthSettings())
    await ctx.startup()
    try:
        await drained(ctx)
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = ctx.plugin_host("hooked").spawn(forever(), name="hooked:forever")
        await started.wait()
        assert task in ctx.plugin_tasks()
    finally:
        await ctx.shutdown()
    assert task.cancelled() or task.done()
    assert ctx.plugin_tasks() == []


# ------------------------------------------------------------------ over HTTP


def test_actions_are_endpoints_and_providers_answer_the_search_field(hooked: Path) -> None:
    with TestClient(create_app(str(hooked)), base_url="http://127.0.0.1") as client:
        answered = client.post("/api/plugins/hooked/actions/echo", json={"x": 1})
        assert answered.status_code == 200
        body = answered.json()
        assert body["echo"] == {"x": 1}
        assert body["launch_root"] == str(client.app.state.ctx.paths.root)

        assert client.post("/api/plugins/hooked/actions/echo").json()["echo"] == {}, "no body is fine"
        refused = client.post("/api/plugins/hooked/actions/echo", json={"fail": True})
        assert refused.status_code == 400 and refused.json()["detail"] == "bad payload"
        assert client.post("/api/plugins/hooked/actions/nope", json={}).status_code == 404
        assert client.post("/api/plugins/nope/actions/echo", json={}).status_code == 404

        providers = client.get("/api/search/providers").json()
        assert {"id": "hooked.find", "plugin_id": "hooked", "label": "Finder"} in providers

        quick = client.get("/api/search", params={"q": "thing"}).json()
        assert quick["query"] == "thing" and quick["deep"] is False
        group = next(g for g in quick["providers"] if g["id"] == "hooked.find")
        assert group["results"] == [
            {
                "title": "hit for thing",
                "subtitle": None,
                "path": "a.txt",
                "line": 3,
                "snippet": None,
                "kind": "file",
                "score": None,
            }
        ]
        assert (group["note"], group["answer"], group["error"]) == ("quick", None, None)

        deep = client.get("/api/search", params={"q": "thing", "deep": "true"}).json()
        assert next(g for g in deep["providers"] if g["id"] == "hooked.find")["note"] == "deep"

        boom = client.get("/api/search", params={"q": "boom"}).json()
        group = next(g for g in boom["providers"] if g["id"] == "hooked.find")
        assert group["results"] == [] and "provider exploded" in group["error"]

        assert client.get("/api/search", params={"q": ""}).status_code == 422

        # Disabling the plugin withdraws both, the same way it withdraws a previewer.
        assert client.post("/api/plugins/hooked/disable").status_code == 200
        assert all(p["plugin_id"] != "hooked" for p in client.get("/api/search/providers").json())
        assert client.post("/api/plugins/hooked/actions/echo", json={}).status_code == 404

"""Plugin environments build themselves (§2.1, §9.4).

Nobody presses a button: a started project builds the environment of every enabled plugin
that declares one, rebuilds it when the requirements change, and a node that runs in it
waits for the build. ``uv`` is faked throughout — what is under test is when the Studio
builds, what it runs, and what it reports, not uv itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from sqlmodel import select

from voxarium_studio.core.config import PluginConfig
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.security import AuthSettings
from voxarium_studio.models.pipeline import NodeRun, NodeStatus, Run, RunStatus
from voxarium_studio.plugins import envs as plugin_envs
from voxarium_studio.plugins.envs import EnvError
from voxarium_studio.plugins.manifest import PluginManifest
from voxarium_studio.schemas.pipeline import PipelineDefinition
from voxarium_studio.ws.events import EventType

pytestmark = pytest.mark.real_plugin_envs

PLUGIN_PY = '''
from voxarium_studio.plugin_api import ExecutionSpec, NodeTypeSchema, PortSchema


def register(registry):
    registry.add_node_type(
        NodeTypeSchema(
            id="{id}.echo",
            name="Echo",
            outputs=[PortSchema(id="text", label="Text")],
            execution=ExecutionSpec(kind="python_script", entry="echo.py", env="plugin_venv"),
        )
    )
'''

ECHO_PY = '''
import json, os
with open(os.environ["VOXARIUM_STUDIO_OUTPUT_FILE"], "w", encoding="utf-8") as handle:
    json.dump({"text": "echoed"}, handle)
'''


def write_plugin(
    directory: Path,
    plugin_id: str = "envy",
    *,
    python: str | None = "3.12",
    enabled: bool = True,
    requirements: str = "numpy\n",
) -> Path:
    root = directory / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {"requirements": "requirements.txt"}
    if python:
        env["python"] = python
    manifest = {
        "id": plugin_id,
        "name": plugin_id.title(),
        "enabled_by_default": enabled,
        "backend": {"entry": "plugin.py:register", "python_env": env},
    }
    (root / "plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    (root / "plugin.py").write_text(PLUGIN_PY.replace("{id}", plugin_id), encoding="utf-8")
    (root / "echo.py").write_text(ECHO_PY, encoding="utf-8")
    return root


def load(root: Path) -> PluginManifest:
    return PluginManifest.from_file(root / "plugin.yaml", scope="project")


class FakeUv:
    """Stands in for `uv venv` / `uv pip install`, recording what the Studio asked for.

    `venv` makes the interpreter file appear, which is all `venv_python()` looks for.
    `gate`, when set, holds every install until the test lets it through.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_install: str | None = None
        self.gate: asyncio.Event | None = None

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]

    async def __call__(self, cmd: list[str], *, cwd: Path, log_fn=None) -> None:
        self.calls.append(cmd)
        if cmd[1] == "venv":
            interpreter = Path(cmd[2]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_text("", encoding="utf-8")
            return
        if log_fn is not None:
            await log_fn("Resolved 1 package")
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_install:
            raise EnvError(self.fail_install)


@pytest.fixture
def uv(monkeypatch: pytest.MonkeyPatch) -> FakeUv:
    fake = FakeUv()
    monkeypatch.setattr(plugin_envs, "uv_command", lambda: ["uv"])
    monkeypatch.setattr(plugin_envs, "_run", fake)
    return fake


async def build(manifest: PluginManifest, **kwargs) -> Path | None:
    task = plugin_envs.schedule(manifest, **kwargs)
    return await task if task is not None else None


# ------------------------------------------------------------------ the builder


async def test_a_declared_environment_is_built_then_left_alone(tmp_path: Path, uv: FakeUv) -> None:
    manifest = load(write_plugin(tmp_path))
    assert plugin_envs.status(manifest) == {"state": "pending", "error": None}

    python = await build(manifest)

    assert python == manifest.venv_python()
    assert uv.verbs() == ["venv", "pip"]
    assert uv.calls[0][-2:] == ["--python", "3.12"]
    assert uv.calls[1][-2:] == ["-r", str(manifest.root / "requirements.txt")]
    assert plugin_envs.status(manifest) == {"state": "ready", "error": None}
    # Current: nothing to schedule, so the next start or enable costs nothing.
    assert plugin_envs.schedule(manifest) is None
    assert uv.verbs() == ["venv", "pip"]


async def test_changed_requirements_are_installed_into_the_same_venv(
    tmp_path: Path, uv: FakeUv
) -> None:
    root = write_plugin(tmp_path)
    manifest = load(root)
    await build(manifest)

    (root / "requirements.txt").write_text("numpy\ntorch\n", encoding="utf-8")
    assert plugin_envs.status(manifest)["state"] == "pending"
    await build(manifest)

    # Installed in place: a multi-GB venv is not thrown away for one added line.
    assert uv.verbs() == ["venv", "pip", "pip"]
    assert plugin_envs.status(manifest)["state"] == "ready"


async def test_another_interpreter_version_recreates_the_venv(tmp_path: Path, uv: FakeUv) -> None:
    root = write_plugin(tmp_path)
    await build(load(root))

    write_plugin(tmp_path, python="3.11")
    manifest = load(root)
    await build(manifest)

    assert uv.verbs() == ["venv", "pip", "venv", "pip"]
    assert uv.calls[2][-2:] == ["--python", "3.11"]


async def test_a_venv_without_a_stamp_is_updated_not_recreated(tmp_path: Path, uv: FakeUv) -> None:
    """Built by hand, or before the Studio stamped them: may hold things nobody declared."""
    manifest = load(write_plugin(tmp_path))
    interpreter = manifest.venv_path / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    (manifest.venv_path / "hand-installed.txt").write_text("keep me", encoding="utf-8")
    assert plugin_envs.status(manifest)["state"] == "pending"

    await build(manifest)

    assert uv.verbs() == ["pip"]
    assert (manifest.venv_path / "hand-installed.txt").exists()
    assert plugin_envs.status(manifest)["state"] == "ready"


async def test_a_failure_is_reported_until_the_requirements_change(
    tmp_path: Path, uv: FakeUv
) -> None:
    root = write_plugin(tmp_path)
    manifest = load(root)
    uv.fail_install = "No solution found when resolving dependencies"

    with pytest.raises(EnvError, match="No solution found"):
        await build(manifest)
    assert plugin_envs.status(manifest) == {
        "state": "failed",
        "error": "No solution found when resolving dependencies",
    }

    (root / "requirements.txt").write_text("numpy>=1.26\n", encoding="utf-8")
    assert plugin_envs.status(manifest)["state"] == "pending"


async def test_concurrent_requests_share_one_build(tmp_path: Path, uv: FakeUv) -> None:
    manifest = load(write_plugin(tmp_path))
    uv.gate = asyncio.Event()

    first = plugin_envs.schedule(manifest)
    second = plugin_envs.schedule(load(manifest.root))
    await asyncio.sleep(0)
    assert first is second
    assert plugin_envs.status(manifest)["state"] == "building"

    uv.gate.set()
    await first
    assert uv.verbs() == ["venv", "pip"]


async def test_a_plugin_without_an_environment_reports_none(tmp_path: Path, uv: FakeUv) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "plugin.yaml").write_text(yaml.safe_dump({"id": "plain", "name": "Plain"}), encoding="utf-8")
    manifest = load(root)

    assert plugin_envs.status(manifest) is None
    assert plugin_envs.schedule(manifest) is None
    assert uv.calls == []


async def test_prepare_waits_for_the_build_and_says_so(tmp_path: Path, uv: FakeUv) -> None:
    manifest = load(write_plugin(tmp_path))
    lines: list[tuple[str, str]] = []

    async def log(line: str, stream: str) -> None:
        lines.append((line, stream))

    await plugin_envs.prepare(manifest, log)

    assert plugin_envs.status(manifest)["state"] == "ready"
    text = [line for line, _ in lines]
    assert text[0].startswith("Waiting for the Envy plugin environment")
    assert "Resolved 1 package" in text
    assert text[-1] == "Plugin environment ready."

    # Already current: a node pays nothing and logs nothing.
    lines.clear()
    await plugin_envs.prepare(manifest, log)
    assert lines == []


async def test_prepare_falls_back_to_the_studio_interpreter_when_the_build_fails(
    tmp_path: Path, uv: FakeUv
) -> None:
    manifest = load(write_plugin(tmp_path))
    uv.fail_install = "network is unreachable"
    lines: list[tuple[str, str]] = []

    async def log(line: str, stream: str) -> None:
        lines.append((line, stream))

    await plugin_envs.prepare(manifest, log)

    line, stream = lines[-1]
    assert stream == "stderr"
    assert "running on the Studio's interpreter" in line
    assert "network is unreachable" in line


async def test_force_rebuilds_a_current_environment_from_scratch(tmp_path: Path, uv: FakeUv) -> None:
    manifest = load(write_plugin(tmp_path))
    await build(manifest)

    python, log = await plugin_envs.provision(manifest, force=True)

    assert python == manifest.venv_python()
    assert uv.verbs() == ["venv", "pip", "venv", "pip"]
    assert log == ["Resolved 1 package"]


# ------------------------------------------------------------------ the project


@pytest.fixture
def fake_provision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Builds that never leave `tmp_path`.

    A started context also schedules the built-in plugins that declare an environment
    (`image_ops` is enabled by default), whose venv is inside this checkout. Those are
    refused here before anything touches disk.
    """
    built: list[str] = []

    async def provision(manifest: PluginManifest, *, force: bool, log_fn) -> Path:
        assert manifest.root is not None
        if not manifest.root.is_relative_to(tmp_path):
            raise EnvError("not built in tests")
        built.append(manifest.id)
        venv = manifest.venv_path
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("", encoding="utf-8")
        (venv / plugin_envs.STAMP_FILENAME).write_text(
            '{"fingerprint": "%s", "python": "3.12"}' % plugin_envs.fingerprint(manifest),
            encoding="utf-8",
        )
        return venv / "bin" / "python"

    monkeypatch.setattr(plugin_envs, "_provision", provision)
    return built


async def settle() -> None:
    for task in [b.task for b in plugin_envs._builds.values() if b.task is not None]:
        await asyncio.gather(task, return_exceptions=True)


def env_events(ctx: AppContext, plugin_id: str) -> list[str]:
    return [
        event.data["plugin"]["env"]["state"]
        for event in ctx.bus.replay(0)
        if event.type == str(EventType.PLUGIN_ENV) and event.data["plugin"]["id"] == plugin_id
    ]


async def test_opening_a_project_builds_enabled_plugins_and_enabling_builds_the_rest(
    project: Path, fake_provision: list[str]
) -> None:
    plugins = ProjectPaths.for_project(project).plugins
    write_plugin(plugins, "envy")
    write_plugin(plugins, "later", enabled=False)
    ctx = AppContext.create(str(project), AuthSettings())
    await ctx.startup()
    try:
        await settle()
        assert fake_provision == ["envy"]
        assert env_events(ctx, "envy") == ["building", "ready"]
        listed = {p.id: p.to_json()["env"] for p in ctx.registry.plugins()}
        assert listed["envy"]["state"] == "ready"
        assert listed["later"]["state"] == "pending"

        ctx.config.plugins.setdefault("later", PluginConfig()).enabled = True
        await ctx.reload_plugins()
        await settle()
        assert fake_provision == ["envy", "later"]
        assert env_events(ctx, "later") == ["building", "ready"]
    finally:
        await ctx.shutdown()


async def test_a_node_waits_for_its_plugin_environment(
    project: Path, monkeypatch: pytest.MonkeyPatch, wait_for_job
) -> None:
    """The node is running and says it is waiting; its script starts only after the build."""
    write_plugin(ProjectPaths.for_project(project).plugins, "envy")
    gate = asyncio.Event()

    async def held(manifest, *, force, log_fn):
        await gate.wait()
        await log_fn("Installed 1 package")
        # No interpreter appears, so the script runs on the Studio's own — this is about
        # the wait, not about which python ran it.
        return Path("unused")

    monkeypatch.setattr(plugin_envs, "_provision", held)
    ctx = AppContext.create(str(project), AuthSettings())
    await ctx.startup()
    try:
        definition = PipelineDefinition.model_validate(
            {"id": "t", "name": "t", "nodes": [{"id": "n1", "type": "envy.echo"}], "edges": []}
        )
        run = await ctx.runner.start(definition)
        for _ in range(100):
            logs = [
                e.data["line"] for e in ctx.bus.replay(0) if e.type == str(EventType.NODE_LOG)
            ]
            if logs:
                break
            await asyncio.sleep(0.02)
        assert logs[0].startswith("Waiting for the Envy plugin environment")

        gate.set()
        await wait_for_job(ctx, run.id)
        async with ctx.db.session() as session:
            assert (await session.get(Run, run.id)).status == RunStatus.SUCCESS
            node = (await session.exec(select(NodeRun).where(NodeRun.run_id == run.id))).one()
        assert node.status == NodeStatus.SUCCESS
        assert node.outputs["text"] == "echoed"
        logs = [e.data["line"] for e in ctx.bus.replay(0) if e.type == str(EventType.NODE_LOG)]
        assert "Installed 1 package" in logs
        assert "Plugin environment ready." in logs
    finally:
        await ctx.shutdown()


@pytest.fixture
def wait_for_job():
    async def wait(ctx: AppContext, job_id: str, timeout: float = 60.0) -> None:
        handle = ctx.jobs.get(job_id)
        if handle is None or handle.task is None:
            return
        done, _ = await asyncio.wait([handle.task], timeout=timeout)
        assert done, f"job {job_id} did not finish within {timeout}s"

    return wait

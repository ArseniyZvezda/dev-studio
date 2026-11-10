"""Isolated per-plugin Python environments, provisioned with ``uv`` (§2.1).

Different plugins routinely need conflicting dependency sets (one pins torch, another
pins something incompatible), so each gets its own venv and its node scripts run in it.

Nobody builds one by hand (§9.4). The Studio builds the environment of every enabled
plugin in the background — when a project opens and when a plugin is enabled — and
rebuilds it when the plugin's requirements change. A node that runs in it waits for that
build rather than starting without it (:func:`prepare`).

What "out of date" means is a stamp written into the venv after a successful install: a
hash of the requirements file and the interpreter version asked for. A venv without one
— built by hand, or by a Studio that predates the stamp — is brought up to date in place.

Builds are keyed by venv path and shared by the whole process, not owned by a project:
several projects stay open at once (§5.1) and the built-in plugins' venvs sit beside their
source, so two projects asking for the same environment must get the one build.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import PluginManifest

log = logging.getLogger(__name__)

LineFn = Callable[[str], Awaitable[None]]
NodeLogFn = Callable[[str, str], Awaitable[None]]
WatchFn = Callable[[Path], Awaitable[None]]

STAMP_FILENAME = ".voxarium-studio-env.json"
LOG_TAIL = 200


class EnvError(RuntimeError):
    pass


@dataclass(eq=False)
class _Build:
    log: list[str] = field(default_factory=list)
    listeners: list[LineFn] = field(default_factory=list)
    running: bool = True
    task: asyncio.Task[Path] | None = None

    async def emit(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-LOG_TAIL]
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                await listener(line)


_builds: dict[Path, _Build] = {}
_failures: dict[Path, tuple[str, str]] = {}
"""The last failed build per venv, with the fingerprint it was attempted for — so a
failure is reported until the requirements change or a build succeeds."""

_watchers: list[WatchFn] = []


def uv_command() -> list[str]:
    """Locate ``uv``, falling back to the module form if it is only pip-installed."""
    exe = shutil.which("uv")
    if exe:
        return [exe]
    try:
        import uv  # noqa: F401
    except ImportError as exc:
        raise EnvError(
            "uv is required to build isolated plugin environments but was not found. "
            "Install it with `pip install uv` or see https://docs.astral.sh/uv/."
        ) from exc
    return [sys.executable, "-m", "uv"]


async def _run(cmd: list[str], *, cwd: Path, log_fn: LineFn | None = None) -> None:
    log.debug("running %s (cwd=%s)", " ".join(cmd), cwd)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    tail: list[str] = []
    try:
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            tail.append(line)
            del tail[:-40]
            if log_fn is not None:
                await log_fn(line)
        returncode = await process.wait()
    except asyncio.CancelledError:
        # A half-done install is redone by the next build: the stamp is written last.
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        raise
    if returncode != 0:
        raise EnvError(
            f"`{' '.join(cmd[:3])} ...` failed with exit code {returncode}:\n"
            + "\n".join(tail[-20:])
        )


# ---------------------------------------------------------------- what is wanted


def fingerprint(manifest: PluginManifest) -> str | None:
    """What the environment is built from, as one hash; ``None`` if it declares none."""
    if manifest.venv_path is None or manifest.root is None or manifest.backend is None:
        return None
    spec = manifest.backend.python_env
    assert spec is not None
    source = spec.requirements or spec.pyproject
    digest = hashlib.sha256()
    digest.update(f"python={spec.python or ''}\nsource={source}\n".encode())
    try:
        digest.update((manifest.root / str(source)).read_bytes())
    except OSError:
        # Hashed as missing, so creating the file later is a change like any other.
        digest.update(b"<missing>")
    return digest.hexdigest()


def _read_stamp(venv: Path) -> dict[str, Any] | None:
    try:
        stamp = json.loads((venv / STAMP_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return stamp if isinstance(stamp, dict) else None


def is_current(manifest: PluginManifest) -> bool:
    """Built, and built from what the plugin declares today."""
    venv = manifest.venv_path
    if venv is None or manifest.venv_python() is None:
        return False
    stamp = _read_stamp(venv)
    return stamp is not None and stamp.get("fingerprint") == fingerprint(manifest)


def status(manifest: PluginManifest) -> dict[str, Any] | None:
    """The environment's state for the Plugins page; ``None`` if the plugin declares none.

    ``pending`` is "not built for the current requirements, and not being built" — what a
    disabled plugin's environment is until the plugin is enabled.
    """
    venv = manifest.venv_path
    if venv is None:
        return None
    if _active(venv) is not None:
        return {"state": "building", "error": None}
    if is_current(manifest):
        return {"state": "ready", "error": None}
    failure = _failures.get(venv)
    if failure is not None and failure[0] == fingerprint(manifest):
        return {"state": "failed", "error": failure[1]}
    return {"state": "pending", "error": None}


# ---------------------------------------------------------------- building


def _active(venv: Path) -> _Build | None:
    """The build in progress for ``venv``. A task cancelled before it ever ran never
    reached the ``finally`` that clears ``running``, hence the second check."""
    build = _builds.get(venv)
    if build is None or not build.running or build.task is None or build.task.done():
        return None
    return build


def watch(callback: WatchFn) -> Callable[[], None]:
    """Be told the venv path whenever a build starts or ends; returns the unsubscribe."""
    _watchers.append(callback)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            _watchers.remove(callback)

    return unsubscribe


async def _notify(venv: Path) -> None:
    for callback in list(_watchers):
        try:
            await callback(venv)
        except Exception:
            log.exception("Plugin environment watcher failed for %s", venv)


def schedule(manifest: PluginManifest, *, force: bool = False) -> asyncio.Task[Path] | None:
    """Start building the environment in the background unless it is already current.

    Returns the build to await — the one already running, if there is one — or ``None``
    when there is nothing to do. ``force`` rebuilds a current environment from scratch.
    """
    venv = manifest.venv_path
    if venv is None:
        return None
    build = _active(venv)
    if build is not None:
        assert build.task is not None
        return build.task
    if not force and is_current(manifest):
        return None

    build = _Build()
    build.task = asyncio.create_task(
        _build(manifest, build, force=force), name=f"plugin-env:{manifest.id}"
    )
    # Retrieved here so a build nobody awaits does not end as "exception never retrieved";
    # its failure is recorded in `_failures` and shown on the Plugins page instead.
    build.task.add_done_callback(lambda task: task.cancelled() or task.exception())
    _builds[venv] = build
    return build.task


async def _build(manifest: PluginManifest, build: _Build, *, force: bool) -> Path:
    venv = manifest.venv_path
    assert venv is not None
    wanted = fingerprint(manifest)
    try:
        await _notify(venv)
        python = await _provision(manifest, force=force, log_fn=build.emit)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        _failures[venv] = (wanted or "", message)
        log.warning("Could not build the environment of plugin %s: %s", manifest.id, message)
        raise EnvError(message) from exc
    else:
        _failures.pop(venv, None)
        log.info("Environment of plugin %s is ready at %s", manifest.id, venv)
        return python
    finally:
        build.running = False
        with contextlib.suppress(Exception):
            await _notify(venv)


async def _provision(manifest: PluginManifest, *, force: bool, log_fn: LineFn) -> Path:
    """Create the venv if it has to be, install the requirements, then stamp it."""
    venv = manifest.venv_path
    root = manifest.root
    assert venv is not None and root is not None and manifest.backend is not None
    spec = manifest.backend.python_env
    assert spec is not None
    wanted = fingerprint(manifest)

    uv = uv_command()
    stamp = _read_stamp(venv)
    python = manifest.venv_python()
    # Only a venv whose stamp names another interpreter is recreated. One with no stamp
    # may be somebody's own, and installing into it is as far as the Studio goes.
    recreate = force or python is None or (
        stamp is not None and stamp.get("python") != spec.python
    )
    if recreate:
        if venv.exists():
            await asyncio.to_thread(shutil.rmtree, venv)
        create_cmd = [*uv, "venv", str(venv)]
        if spec.python:
            create_cmd += ["--python", spec.python]
        await _run(create_cmd, cwd=root, log_fn=log_fn)
        python = manifest.venv_python()
        if python is None:
            raise EnvError(f"uv created {venv} but no interpreter was found inside it")
    else:
        # Until the install below succeeds, this venv is not what the plugin asks for.
        with contextlib.suppress(OSError):
            (venv / STAMP_FILENAME).unlink()
    assert python is not None

    install_cmd = [*uv, "pip", "install", "--python", str(python)]
    if spec.requirements:
        requirements = root / spec.requirements
        if not requirements.exists():
            raise EnvError(f"requirements file not found: {requirements}")
        install_cmd += ["-r", str(requirements)]
    else:
        install_cmd += ["-e", str((root / str(spec.pyproject)).parent)]
    await _run(install_cmd, cwd=root, log_fn=log_fn)

    (venv / STAMP_FILENAME).write_text(
        json.dumps({"fingerprint": wanted, "python": spec.python}), encoding="utf-8"
    )
    return python


async def provision(
    manifest: PluginManifest, *, force: bool = False
) -> tuple[Path | None, list[str]]:
    """Build now and wait: the interpreter (``None`` if no environment) and the build log."""
    if manifest.venv_path is None:
        return None, []
    task = schedule(manifest, force=force)
    if task is None:
        return manifest.venv_python(), []
    build = _builds[manifest.venv_path]
    python = await asyncio.shield(task)
    return python, list(build.log)


async def prepare(manifest: PluginManifest, log_fn: NodeLogFn) -> None:
    """Before a node runs in the plugin's environment: wait for it to be current.

    A build that fails does not fail the node. It runs on the Studio's interpreter, as it
    always could, and its log says why — the plugins that declare an environment are
    written to degrade without one (``hunyuan3d``, ``image_ops``).

    The wait is shielded: cancelling the node leaves the build running for the next one.
    """
    venv = manifest.venv_path
    if venv is None:
        return
    task = schedule(manifest)
    if task is None:
        return
    build = _builds[venv]

    async def forward(line: str) -> None:
        await log_fn(line, "stdout")

    await log_fn(f"Waiting for the {manifest.name} plugin environment to be built (uv)…", "stdout")
    build.listeners.append(forward)
    try:
        await asyncio.shield(task)
    except EnvError as exc:
        await log_fn(
            "The plugin environment could not be built; running on the Studio's interpreter "
            f"instead.\n{exc}",
            "stderr",
        )
    else:
        await log_fn("Plugin environment ready.", "stdout")
    finally:
        with contextlib.suppress(ValueError):
            build.listeners.remove(forward)


async def shutdown() -> None:
    """Stop every build in progress — the server is going away (§5.1)."""
    tasks = [build.task for venv in list(_builds) if (build := _active(venv)) and build.task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def remove_env(manifest: PluginManifest) -> None:
    venv_path = manifest.venv_path
    if venv_path is None or not venv_path.exists():
        return
    await asyncio.to_thread(shutil.rmtree, venv_path, True)

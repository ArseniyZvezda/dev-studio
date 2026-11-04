"""Isolated per-plugin Python environments, provisioned with ``uv`` (§2.1).

Different plugins routinely need conflicting dependency sets (one pins torch, another
pins something incompatible), so each gets its own venv and its node scripts run in it.
``uv`` is used over ``venv``+``pip`` because installs happen interactively from the UI.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from .manifest import PluginManifest

log = logging.getLogger(__name__)

LogFn = Callable[[str], Awaitable[None]] | None


class EnvError(RuntimeError):
    pass


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


async def _run(cmd: list[str], *, cwd: Path, log_fn: LogFn = None) -> None:
    log.debug("running %s (cwd=%s)", " ".join(cmd), cwd)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    tail: list[str] = []
    async for raw in process.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        tail.append(line)
        del tail[:-40]
        if log_fn is not None:
            await log_fn(line)
    returncode = await process.wait()
    if returncode != 0:
        raise EnvError(
            f"`{' '.join(cmd[:3])} ...` failed with exit code {returncode}:\n"
            + "\n".join(tail[-20:])
        )


async def ensure_env(
    manifest: PluginManifest, *, force: bool = False, log_fn: LogFn = None
) -> Path | None:
    """Create/refresh the plugin's venv and install its requirements.

    Returns the interpreter path, or ``None`` when the plugin declares no environment.
    Deliberately *not* called during start-up — a multi-GB install must not block boot.
    """
    venv_path = manifest.venv_path
    if venv_path is None or manifest.root is None or manifest.backend is None:
        return None
    env_spec = manifest.backend.python_env
    assert env_spec is not None

    existing = manifest.venv_python()
    if existing is not None and not force:
        return existing

    uv = uv_command()
    create_cmd = [*uv, "venv", str(venv_path)]
    if env_spec.python:
        create_cmd += ["--python", env_spec.python]
    await _run(create_cmd, cwd=manifest.root, log_fn=log_fn)

    python_path = manifest.venv_python()
    if python_path is None:
        raise EnvError(f"uv created {venv_path} but no interpreter was found inside it")

    install_cmd = [*uv, "pip", "install", "--python", str(python_path)]
    if env_spec.requirements:
        requirements = manifest.root / env_spec.requirements
        if not requirements.exists():
            raise EnvError(f"requirements file not found: {requirements}")
        install_cmd += ["-r", str(requirements)]
    elif env_spec.pyproject:
        install_cmd += ["-e", str((manifest.root / env_spec.pyproject).parent)]
    else:
        return python_path

    await _run(install_cmd, cwd=manifest.root, log_fn=log_fn)
    log.info("Provisioned environment for plugin %s at %s", manifest.id, venv_path)
    return python_path


async def remove_env(manifest: PluginManifest) -> None:
    venv_path = manifest.venv_path
    if venv_path is None or not venv_path.exists():
        return
    await asyncio.to_thread(shutil.rmtree, venv_path, True)

"""Recover a frontend/backend version mismatch without pulling or rebuilding (§2.5)."""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from ..build_info import build_info, studio_repo_root
from ..updates import RelaunchTarget, UpdateService
from ..updates.versions import parse_tag
from .deps import Ctx

router = APIRouter(prefix="/runtime", tags=["runtime"])


class RecoverRequest(BaseModel):
    frontend_version: str = Field(min_length=1, max_length=100)
    backend_version: str = Field(min_length=1, max_length=100)


def available_versions(*, dev: bool) -> tuple[str, str, Path | None]:
    """Read installed files, not the version imported by this old process."""
    from ..main import static_dir

    package = Path(__file__).resolve().parents[1]
    tree = ast.parse((package / "__init__.py").read_text(encoding="utf-8"))
    backend = next(ast.literal_eval(node.value) for node in tree.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets))
    root = studio_repo_root()
    if dev and root:
        manifest = root / "frontend" / "package.json"
    else:
        directory = static_dir()
        if directory is None:
            raise ValueError("The frontend is not built. Build or reinstall the Studio first.")
        manifest = directory / "studio-version.json"
    frontend = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    if not isinstance(backend, str) or not isinstance(frontend, str):
        raise ValueError("Invalid installed version metadata.")
    # The packaged desktop puts run.ps1 alongside python/, just as the checkout does.
    if root is None:
        candidate = Path(sys.executable).resolve().parent.parent
        if (candidate / "run.ps1").is_file():
            root = candidate
    return backend, frontend, root


@router.post("/recover")
async def recover_runtime(body: RecoverRequest, ctx: Ctx, request: Request,
                          background: BackgroundTasks) -> dict[str, str]:
    running = build_info().version
    if running != body.backend_version or running == body.frontend_version:
        return {"action": "reload", "version": running}
    prior = getattr(request.app.state, "runtime_recovery", None)
    if prior:
        if prior.get("error"):
            raise HTTPException(409, prior["error"])
        return {"action": "restart", "version": prior["version"]}
    try:
        installed, frontend, root = available_versions(dev=bool(request.app.state.dev_origins))
    except (OSError, ValueError, KeyError, StopIteration, SyntaxError) as exc:
        raise HTTPException(409, "Cannot read the installed Studio versions. Rebuild or reinstall the Studio.") from exc
    if installed != frontend:
        raise HTTPException(409, "The frontend and backend files have different versions. Rebuild or reinstall the Studio.")
    if installed == running:
        return {"action": "reload", "version": running}
    current_release, installed_release = parse_tag(running), parse_tag(installed)
    if not current_release or not installed_release or installed_release <= current_release:
        raise HTTPException(409, "The installed backend is older than the running server or its version is unknown. Update the installed files first.")
    if root is None:
        raise HTTPException(409, "This installation has no Studio launcher. Restart the server manually.")

    server = request.scope.get("server") or (None, None)
    target = RelaunchTarget(project=ctx.paths.root, host=server[0] or "127.0.0.1",
                            port=int(server[1] or 8765), dev=bool(request.app.state.dev_origins),
                            token=ctx.auth.token)
    # One restart across all tabs/projects. No await between checking and reserving it.
    state = {"version": installed}
    request.app.state.runtime_recovery = state
    run_dir = os.environ.get("VOXARIUM_STUDIO_RUN_DIR")
    service = UpdateService(bus=ctx.bus, config=ctx.config.updates, root=root,
                            work_dir=Path(run_dir) / "recovery" if run_dir else None)

    async def restart() -> None:
        try:
            # The response is sent first. Reuse the orphaned relauncher and run.ps1:
            # shutdown flushes the DB; the launcher stops the old process tree if needed.
            await service.restart_current_files(target)
        except Exception as exc:
            state["error"] = f"Studio restart failed: {exc}"

    background.add_task(restart)
    return {"action": "restart", "version": installed}

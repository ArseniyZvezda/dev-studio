from fastapi import APIRouter, Depends

from ..core.security import require_auth
from .deps import get_ctx
from . import (
    ai_access,
    attachments,
    automations,
    chats,
    desktop,
    docs,
    explorer,
    fs,
    mcp,
    memory,
    pipelines,
    plugins,
    project,
    runtime,
    search,
    skills,
    sync,
    teams,
    updates,
    vcs,
)

api_router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
api_router.include_router(ai_access.router)
api_router.include_router(project.router)
api_router.include_router(runtime.router)
api_router.include_router(desktop.router)
api_router.include_router(fs.router)
api_router.include_router(explorer.router)
api_router.include_router(pipelines.router)
api_router.include_router(plugins.router)
api_router.include_router(search.router)
api_router.include_router(teams.router)
api_router.include_router(chats.router)
api_router.include_router(attachments.router)
api_router.include_router(docs.router)
api_router.include_router(sync.router)
api_router.include_router(skills.router)
api_router.include_router(memory.router)
api_router.include_router(memory.agents_router)
api_router.include_router(automations.router)
api_router.include_router(mcp.router)
api_router.include_router(vcs.router)
api_router.include_router(updates.router)

# §5.3.2–5.3.3: reuse project handlers, not the mutable legacy adapter or global
# operations (picker, application settings, updates, runtime recovery). The binding
# dependency also guards project handlers that do not themselves need a Ctx.
api_router.include_router(project.projects_router)
project_router = APIRouter(
    prefix="/projects/{project_id}", dependencies=[Depends(get_ctx), Depends(sync.bind_http_editor_inventory)],
)
project_router.include_router(project.scoped_router)
for module in (
    explorer, pipelines, search, attachments, docs, sync, skills,
    memory, automations, mcp, vcs, teams, chats,
):
    project_router.include_router(module.router)
project_router.include_router(memory.agents_router)
project_router.include_router(sync.remote_router)
# Environment builds are process-global; only project plugin operations/resources
# are mirrored. Keep the legacy provisioning URL for existing callers (§2.1).
project_plugins = APIRouter()
project_plugins.routes.extend(
    route for route in plugins.router.routes
    if route.path != "/plugins/{plugin_id}/install-env"
)
project_router.include_router(project_plugins)
api_router.include_router(project_router)

__all__ = ["api_router"]

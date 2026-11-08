from fastapi import APIRouter, Depends

from ..core.security import require_auth
from . import (
    attachments,
    automations,
    chats,
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
    teams,
    updates,
    vcs,
)

api_router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])
api_router.include_router(project.router)
api_router.include_router(runtime.router)
api_router.include_router(fs.router)
api_router.include_router(explorer.router)
api_router.include_router(pipelines.router)
api_router.include_router(plugins.router)
api_router.include_router(search.router)
api_router.include_router(teams.router)
api_router.include_router(chats.router)
api_router.include_router(attachments.router)
api_router.include_router(docs.router)
api_router.include_router(skills.router)
api_router.include_router(memory.router)
api_router.include_router(automations.router)
api_router.include_router(mcp.router)
api_router.include_router(vcs.router)
api_router.include_router(updates.router)

__all__ = ["api_router"]

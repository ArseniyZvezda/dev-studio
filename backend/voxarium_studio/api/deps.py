from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from ..core.context import AppContext
from ..core.project_scope import resolve_project


def get_ctx(request: Request) -> AppContext:
    """Bind once per request, never re-read legacy selection (§5.3.2–5.3.3).

    The context is also retained by handlers' stream/background closures. Resident
    contexts are not retired when a tab closes or the legacy selection changes.
    Only the URL is a selector; headers/query parameters cannot rebind a request.
    """
    if not hasattr(request.state, "ctx"):
        project_id = request.path_params.get("project_id")
        if project_id is None:
            request.state.ctx = request.app.state.ctx
        else:
            try:
                scope = resolve_project(request.app.state, project_id)
            except KeyError as exc:
                raise HTTPException(404, "Unknown or expired project id.") from exc
            request.state.project_scope = scope
            request.state.ctx = scope.ctx
    return request.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]

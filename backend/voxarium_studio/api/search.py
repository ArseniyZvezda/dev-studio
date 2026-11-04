"""The search field in the rail, fanned out to plugin search providers (§6.1, §9.7).

The Studio itself indexes nothing. A provider is something a plugin registered —
``graphify`` answers from the knowledge graph it keeps of the project's code — and the
field only appears once at least one is there, so a project with no provider has no dead
control. Every provider is asked at once and each answers for itself: one failing, or
one being slow on a ``deep`` query, must not take the others' hits with it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Query

from ..plugins.api import SearchQuery, SearchResponse
from ..plugins.registry import SearchProvider
from .deps import Ctx

log = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


def provider_json(provider: SearchProvider) -> dict[str, Any]:
    return {
        "id": provider.key,
        "plugin_id": provider.plugin_id,
        "label": provider.label or provider.id,
    }


@router.get("/providers")
async def list_search_providers(ctx: Ctx) -> list[dict[str, Any]]:
    """Who answers the search field. Empty means the field is not shown."""
    return [provider_json(provider) for provider in ctx.registry.search_providers()]


@router.get("")
async def search(
    ctx: Ctx,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    deep: bool = False,
) -> dict[str, Any]:
    """One query, every provider, grouped by provider in the answer.

    ``deep`` is Enter rather than a keystroke: the provider may spend seconds on it and
    answer with prose under its hits. A provider that raises is reported in its own group
    as ``error`` rather than failing the request — the field is typed into continuously,
    and the other providers' hits are still worth showing.
    """
    query = SearchQuery(text=q.strip(), limit=limit, deep=deep)

    async def ask(provider: SearchProvider) -> dict[str, Any]:
        group = provider_json(provider)
        try:
            answer = await provider.handler(ctx.plugin_host(provider.plugin_id), query)
        except Exception as exc:
            log.exception("Search provider %s failed", provider.key)
            return {
                **group,
                "results": [],
                "answer": None,
                "note": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        response = (
            answer
            if isinstance(answer, SearchResponse)
            else SearchResponse(results=list(answer or []))
        )
        return {**group, **response.to_json(), "error": None}

    groups = await asyncio.gather(*(ask(provider) for provider in ctx.registry.search_providers()))
    return {"query": query.text, "deep": deep, "providers": list(groups)}

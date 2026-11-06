"""Shared effort vocabulary for member/task settings and Producer tools (§10.3, §10.11)."""
from __future__ import annotations

from typing import get_args


def effort_levels() -> list[str]:
    """Keep explicit task choices aligned with the installed SDK and settings UI."""
    try:
        from claude_agent_sdk.types import EffortLevel

        return [str(value) for value in get_args(EffortLevel)]
    except Exception:  # SDK absent, or too old to have the parameter
        return ["low", "medium", "high", "xhigh", "max"]

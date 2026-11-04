"""Keeping the Studio up to date with its own repository (§12.1)."""

from .service import (
    RelaunchTarget,
    UpdateError,
    UpdateRefused,
    UpdateService,
    UpdateState,
    running_work,
)
from .versions import Release, latest_release, parse_tag

__all__ = [
    "Release",
    "RelaunchTarget",
    "UpdateError",
    "UpdateRefused",
    "UpdateService",
    "UpdateState",
    "latest_release",
    "parse_tag",
    "running_work",
]

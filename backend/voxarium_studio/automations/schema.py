"""An automation: one trigger, one action (§6.5).

Deliberately that small. "On task → status changed to Needs review → send a notification" is
the whole sentence the page writes and the whole sentence the file holds; conditions, several
actions and branching are what a pipeline is for, and an automation may start one.

The kinds are closed sets here and everything inside a kind is plain data, so the YAML stays
readable and a file written by a newer Studio fails entry by entry (see ``store.py``) rather
than whole.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from ..models.base import new_id
from ..models.teams import TaskStatus

CHANGE_KINDS = ("added", "modified", "deleted")

MAX_NAME = 120
MAX_TEXT = 2000


# --------------------------------------------------------------------------- triggers


class TaskStatusTrigger(BaseModel):
    """A task's status changed — to ``status``, or to anything when it is ``None``."""

    kind: Literal["task.status_changed"] = "task.status_changed"
    status: TaskStatus | None = None


class TaskCreatedTrigger(BaseModel):
    kind: Literal["task.created"] = "task.created"


class PathTrigger(BaseModel):
    """Something under a folder of the managed project (or one file) changed.

    ``path`` is project-relative with forward slashes and no ``./`` — ``models`` for what the
    page shows as ``./models/``; empty means the whole project. What the Studio never watches
    is never seen here either: ``explorer.ignore``, and ``.voxarium_studio/runs``, ``assets`` and
    ``worktrees`` (``explorer/watcher.py``).
    """

    kind: Literal["path.changed"] = "path.changed"
    path: str = ""
    pattern: str = ""
    """A glob on the file's name — ``*.fbx`` — or on its path under ``path`` when it holds a
    slash. Empty matches everything."""

    changes: list[Literal["added", "modified", "deleted"]] = Field(
        default_factory=lambda: list(CHANGE_KINDS)
    )

    @field_validator("path")
    @classmethod
    def _normalise_path(cls, value: str) -> str:
        return normalise_path(value)

    @field_validator("pattern")
    @classmethod
    def _trim_pattern(cls, value: str) -> str:
        return value.strip()

    @field_validator("changes")
    @classmethod
    def _some_change(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Pick at least one kind of change")
        # The order the page lists them in, without repeats, so a save is stable in a diff.
        return [kind for kind in CHANGE_KINDS if kind in value]

    def matches(self, path: str, change: str) -> bool:
        """Whether one watcher entry — a project-relative path and what happened to it — counts."""
        if change not in self.changes:
            return False
        if self.path:
            if path != self.path and not path.startswith(f"{self.path}/"):
                return False
            inner = path[len(self.path) :].lstrip("/")
        else:
            inner = path
        if not self.pattern:
            return True
        # Case-folded: the projects this watches live on Windows and macOS as often as not,
        # where `Hero.FBX` is the same file as `hero.fbx` to everything else that touches it.
        subject = inner if "/" in self.pattern else inner.rsplit("/", 1)[-1]
        return fnmatchcase(subject.casefold(), self.pattern.casefold())


class RunFinishedTrigger(BaseModel):
    """A pipeline run ended — ``pipeline_id`` narrows it to one pipeline."""

    kind: Literal["run.finished"] = "run.finished"
    pipeline_id: str | None = None
    outcome: Literal["any", "success", "failed"] = "any"
    """``any`` includes a cancelled run; the other two do not."""


Trigger = Annotated[
    TaskStatusTrigger | TaskCreatedTrigger | PathTrigger | RunFinishedTrigger,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- actions


class NotifyAction(BaseModel):
    """Tell every open Studio window. Both texts take ``{placeholders}`` (``engine.py``);
    left empty, the window words the notification itself, in its own language."""

    kind: Literal["notify"] = "notify"
    title: str = Field(default="", max_length=MAX_NAME)
    message: str = Field(default="", max_length=MAX_TEXT)


class RunPipelineAction(BaseModel):
    kind: Literal["pipeline.run"] = "pipeline.run"
    pipeline_id: str = Field(min_length=1)
    use_cache: bool = True


class PluginAction(BaseModel):
    """One of the actions plugins offer (``registry.add_automation_action``, §9.7)."""

    kind: Literal["plugin.action"] = "plugin.action"
    plugin_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)


Action = Annotated[NotifyAction | RunPipelineAction | PluginAction, Field(discriminator="kind")]


# --------------------------------------------------------------------------- the automation


class Automation(BaseModel):
    id: str = Field(default_factory=lambda: new_id("auto"))
    name: str = Field(default="", max_length=MAX_NAME)
    """Optional: the page reads the trigger and the action out as a sentence either way."""

    enabled: bool = True
    trigger: Trigger
    action: Action

    @field_validator("id")
    @classmethod
    def _plain_id(cls, value: str) -> str:
        # Hand-written ids are welcome (`notify-review` reads better in a diff than a hex
        # string); they travel in URLs, so they are held to what needs no escaping.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError("An id is letters, digits, '_', '-' and '.', 64 at most")
        return value

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        return value.strip()


class AutomationInput(BaseModel):
    """What the page sends: an automation without an id of its own choosing."""

    name: str = Field(default="", max_length=MAX_NAME)
    enabled: bool = True
    trigger: Trigger
    action: Action


def normalise_path(value: str) -> str:
    """``./models/`` → ``models``; refuses anything that is not a path *inside* the project.

    Only the spelling is checked — the folder need not exist yet, since "when something
    appears in ``Export/``" is a fair thing to ask before the first export.
    """
    text = value.strip().replace("\\", "/")
    if re.match(r"^[A-Za-z]:", text) or text.startswith("/"):
        raise ValueError("The path is relative to the project, e.g. ./models/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("The path may not leave the project")
    return "/".join(parts)

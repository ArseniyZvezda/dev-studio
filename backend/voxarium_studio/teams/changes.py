"""What one task changed, for the reviewer's column beside its transcript (§10.4, §10.5).

Two sources, and which one answers is a property of the task rather than a setting:

* **Its isolated workspace**, whenever it has one. That is the only place changes can
  honestly be attributed to a single task — the whole point of §10.5 — and the backend is
  asked for them through :meth:`~voxarium_studio.vcs.base.VcsBackend.workspace_changes`, so a
  Plastic project answers the same question as a git one.
* **The files its own tool calls named**, for every task that has no workspace: a person's
  task, a project under no version control, a member with ``isolate_worktree`` off. Those
  tasks edit the shared working copy, where one task's changes and the next one's are the
  same changes, so a `git status` of the project would be an answer to a different
  question. The transcript, on the other hand, records exactly which files *this* task
  wrote — with no line counts and no idea whether the edit survived, which is why the two
  sources are labelled rather than blended.

The fallback also includes separately labelled disk observations captured during shared
workspace runs (§10.4). Those establish timing, never authorship, and are excluded from
automatic commit drafts unless a writing tool independently named the same file.
Both kinds of evidence read the stored transcript and survive a Studio restart.

:func:`edit_stat` is the third thing a tool call can be asked, and the one that has to be
asked *early* — see its own note.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core.paths import current_data_path
from ..models.teams import AgentMessage
from ..vcs import VcsBackend, VcsError

log = logging.getLogger(__name__)

WRITING_TOOLS = frozenset(
    {
        "edit",
        "write",
        "multiedit",
        "notebookedit",
        "write_doc",
    }
)
"""Tools whose call means "this file now differs".

Matched on the last segment of the name, so an MCP tool (``mcp__voxarium_studio__write_doc``)
is recognised as itself. Deliberately excludes ``read``, ``grep``, ``glob`` and ``bash``:
the first three touch nothing, and a shell command *may* have written anything at all —
listing what it mentioned would be a guess presented as a fact."""

_PATH_KEYS = ("file_path", "path", "notebook_path")
"""Input keys naming the file, in the order the tools themselves prefer."""


def tool_name(data: dict[str, Any]) -> str:
    """The bare tool name, MCP prefix and server stripped — mirrors `lib/activity.ts`."""
    raw = data.get("name")
    if not isinstance(raw, str) or not raw:
        return ""
    return raw.rsplit("__", 1)[-1] if raw.startswith("mcp__") else raw


def edit_stat(name: str, tool_input: Any) -> dict[str, int] | None:
    """Lines one writing call adds and removes, or ``None`` when it cannot be told.

    Asked at capture time, in the backend, because that is the only moment the *whole*
    input exists: ``_summarise_tool_input`` clips every value to 500 characters before the
    call is stored, which is plenty to name a file and far too little to diff one. The
    answer rides along on the message as ``data["edit"]``, and the transcript reads it
    back rather than recomputing it.

    A **line-level diff**, not "lines in, lines out". An ``Edit`` carries enough
    surrounding context to make its match unique, so measuring both strings whole would
    report a one-word fix as thirty lines rewritten. ``Write`` has no old side to diff
    against — the file may not have existed — so it counts what it wrote and claims
    nothing about what it replaced, and a call the shape of which says nothing about lines
    at all answers ``None`` rather than zero.

    What this counts is what the *call* asked for, which is not the same claim as the
    changes column beside it: that one diffs the workspace and knows whether an edit
    survived (§10.5). ``replace_all`` is therefore counted once — how many occurrences a
    file holds is a property of the file, not of the call.
    """
    tool = tool_name({"name": name}).casefold()
    if tool not in WRITING_TOOLS or not isinstance(tool_input, dict):
        return None
    if tool == "edit":
        return _diff_stat(tool_input.get("old_string"), tool_input.get("new_string"))
    if tool == "multiedit":
        return _multi_edit_stat(tool_input.get("edits"))
    if tool in ("write", "write_doc"):
        return _diff_stat("", tool_input.get("content"))
    if tool == "notebookedit":
        return _diff_stat("", tool_input.get("new_source"))
    return None


def _multi_edit_stat(edits: Any) -> dict[str, int] | None:
    """One figure for the whole call: the transcript shows one line for it.

    All or nothing — an entry that cannot be read makes the total wrong rather than
    smaller, and a wrong count is worse than none in a panel a reviewer reads as evidence.
    """
    if not isinstance(edits, list) or not edits:
        return None
    total = {"insertions": 0, "deletions": 0}
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        part = _diff_stat(edit.get("old_string"), edit.get("new_string"))
        if part is None:
            return None
        total["insertions"] += part["insertions"]
        total["deletions"] += part["deletions"]
    return total


def _diff_stat(old: Any, new: Any) -> dict[str, int] | None:
    """`git diff --numstat` over two fragments: only the lines that actually differ."""
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    before, after = old.splitlines(), new.splitlines()
    insertions = deletions = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for opcode, start_old, end_old, start_new, end_new in matcher.get_opcodes():
        if opcode == "equal":
            continue
        deletions += end_old - start_old
        insertions += end_new - start_new
    return {"insertions": insertions, "deletions": deletions}


def files_touched(
    messages: list[AgentMessage], root: Path, *, docs: Path | None = None
) -> list[str]:
    """Project-relative paths this task's tool calls wrote to, first mention first.

    Order is the transcript's rather than alphabetical: the sequence a task worked in is
    information, and it is the ordering this source can honestly offer — the line counts
    beside it (:func:`edit_totals`) are what the calls *asked for*, not a measurement of
    the files, and a list sorted by them would rank a claim.

    ``docs`` is the Documents folder (``.voxarium_studio/docs``): a ``write_doc`` call names its
    file relative to **that**, not to the project — ``art/keycap.md`` is
    ``.voxarium_studio/docs/art/keycap.md`` on disk — and a list that repeated the tool's own frame
    named a file that does not exist, which the Commit button then handed to the version
    control tool (Plastic answered "not changed in current workspace").
    """
    seen: dict[str, None] = {}
    for path, _data in _writes(messages, root, docs):
        seen.setdefault(path, None)
    return list(seen)


def edit_totals(
    messages: list[AgentMessage], root: Path, *, docs: Path | None = None
) -> dict[str, dict[str, int] | None]:
    """Lines added and removed per touched file, summed over every call that wrote it.

    Read off ``data["edit"]`` — what :func:`edit_stat` recorded at capture time — and
    never recomputed: the stored input is clipped, so the figures exist nowhere else.
    All or nothing per file, on the rule :func:`_multi_edit_stat` already follows for one
    call: a file with even one unmeasured write (a transcript from before the counting
    existed, a call whose shape says nothing about lines) answers ``None`` rather than a
    total that is quietly short. The column draws ``None`` as no figure, which is the
    honest answer, where a small number would read as a small change.

    Still a claim about the *calls*, not the file (§10.4): two edits that undo each other
    sum to a change the workspace would not show. The ``workspace`` source is the
    measurement; this is what the transcript can say.
    """
    totals: dict[str, dict[str, int] | None] = {}
    for path, data in _writes(messages, root, docs):
        if path in totals and totals[path] is None:
            continue
        stat = data.get("edit")
        if not isinstance(stat, dict):
            totals[path] = None
            continue
        insertions, deletions = stat.get("insertions"), stat.get("deletions")
        if not isinstance(insertions, int) or not isinstance(deletions, int):
            totals[path] = None
            continue
        current = totals.setdefault(path, {"insertions": 0, "deletions": 0})
        assert current is not None
        current["insertions"] += insertions
        current["deletions"] += deletions
    return totals


def observed_files(messages: list[AgentMessage], root: Path) -> list[str]:
    """Durable disk observations, explicitly without an authorship claim (§10.4)."""
    paths: dict[str, None] = {}
    for message in messages:
        if message.kind != "file_observation" or not isinstance(message.data, dict):
            continue
        recorded = message.data.get("paths")
        if isinstance(recorded, list):
            for path in recorded:
                if isinstance(path, str) and path:
                    paths.setdefault(current_data_path(path, root), None)
    return list(paths)


def _writes(
    messages: list[AgentMessage], root: Path, docs: Path | None
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every writing call in transcript order, as ``(project-relative path, data)``."""
    for message in messages:
        if message.kind != "tool_use":
            continue
        data = message.data if isinstance(message.data, dict) else {}
        tool = tool_name(data).casefold()
        if tool not in WRITING_TOOLS:
            continue
        target = _input_path(data)
        if not target:
            continue
        if tool == "write_doc" and docs is not None and not Path(target).is_absolute():
            target = str(docs / target)
        yield _relative(target, root), data


def _input_path(data: dict[str, Any]) -> str:
    raw_input = data.get("input")
    if not isinstance(raw_input, dict):
        return ""
    for key in _PATH_KEYS:
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _relative(path: str, root: Path) -> str:
    """Project-relative and forward-slashed where it can be; verbatim where it cannot.

    An agent writes absolute paths, and a task whose ``launch_root`` is above the project
    (§10.5) can legitimately write outside it — that path is shown as it is rather than
    turned into a `../..` chain nothing else in the UI uses.
    """
    path = current_data_path(path, root)
    candidate = Path(path)
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return candidate.as_posix() if candidate.is_absolute() else path.replace("\\", "/")


def is_outside(path: str, root: Path, workdir: Path) -> bool:
    """Whether a listed path lies outside the directory the agent was started in (§10.4).

    The column splits its files into *Project* and *Outside project* on this answer, and
    the frame is the session's **working directory**, not the managed project: a task
    started one level up (``teams.launch_root: ..``) legitimately edits the siblings, and
    those are the project as far as that session was concerned. ``path`` is as the list
    reports it — relative to ``root`` where it could be, absolute where it could not.
    """
    candidate = Path(path)
    absolute = candidate if candidate.is_absolute() else root / candidate
    try:
        absolute.resolve().relative_to(workdir.resolve())
    except (ValueError, OSError):
        return True
    return False


async def workspace_changes(
    vcs: VcsBackend | None, worktree: Path
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """``(files, branch, error)`` for one isolated workspace.

    A tool that refuses is reported rather than swallowed: an empty list and a reason the
    user can act on are different from an empty list and silence, and this column is read
    as evidence about a task.
    """
    if vcs is None:
        return [], None, None
    try:
        status = await vcs.workspace_changes(worktree)
    except VcsError as exc:
        log.warning("Could not read changes in %s: %s", worktree, exc)
        return [], None, str(exc)
    return [change.to_json() for change in status.changes], status.branch, None

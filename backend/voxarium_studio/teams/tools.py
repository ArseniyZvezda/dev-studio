"""Studio tools handed to an agent team member (§10.4).

A member can work the task board, delegate, write shared documents, and author project
pipelines, automations and skills (§10.7, ``authoring.py``). All of them are
exposed as an in-process MCP server, so the tools run inside the Studio — same event bus,
same database, same routing rules as a click in the UI — rather than as a subprocess the
agent could reach around.

Routing is re-checked here on every call: an agent asking to assign work is exactly as
constrained as its badge allows (``check_can_assign``), an edit goes through
``check_can_edit`` and a note through ``check_can_act``, because tool arguments are model
output and carry no authority of their own. Nothing here reimplements a query or a rule —
board tools use the same ``TeamOrchestrator`` methods as REST; authoring uses the page's stores.

Tasks are named to the agent by their **display id** (``VS-01``, §10.2) rather than by the
opaque primary key: it is what the brief it was given says, what a person says in a note,
and what fits in a sentence. Every tool that takes a task accepts either, plus a bare
number, through :meth:`TeamOrchestrator.find_task`.

A description here says what one tool does; *when* and *how* to use them together — decompose
before delegating, read a document before replacing it, never wait on a task in ``todo`` — is
the Studio skill's (``skill_bundle/skills/studio/SKILL.md``, §10.3). A tool added here is a
tool the skill has to name, and ``test_studio_skill.py`` fails until it does.

The whole module degrades to ``None`` when ``claude-agent-sdk`` is absent, which is what
lets the stub backends in the smoke tests run without it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlmodel import select

from ..models.teams import (
    TERMINAL_TASK_STATUSES,
    MemberKind,
    TaskResultType,
    TaskStatus,
    TeamMember,
    TeamTask,
)
from .docs import DocStore

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .orchestrator import TeamOrchestrator

log = logging.getLogger(__name__)

# Producer-generated task descriptions stay concise (§10.11).
PRODUCER_BRIEF_MAX_CHARS = 1500

SERVER_NAME = "voxarium_studio"
"""MCP server name. The CLI exposes its tools as ``mcp__voxarium_studio__<tool>``.

Also the name the MCP Servers page lists this server under (§10.7). It is **not** in
``.mcp.json`` — the server is in-process and built from the running orchestrator — so the
page marks it built-in and refuses to delete it, while ``config.mcp.disabled`` switches it
off exactly as it does any other server.
"""

#: The statuses an agent may move its own task into with ``update_task``.
#:
#: Deliberately narrower than what the REST endpoint allows the same actor (§10.4): the
#: manual status move exists for a person maintaining the board, and `check_can_edit` lets
#: the assignee use it. An *agent* reaching `done` on its own work would be approving it,
#: which is its manager's call — and it does not need to, because finishing a run already
#: sends the task to `needs_review`.
_AGENT_FORBIDDEN_SELF_STATUSES = TERMINAL_TASK_STATUSES


@dataclass(slots=True)
class StudioToolset:
    """What the backend needs to hand the SDK: the server plus the tool names to allow."""

    mcp_servers: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    definitions: list[Any] = field(default_factory=list)
    """The ``SdkMcpTool`` objects behind the server, exposed so the tools can be exercised
    directly — calling ``definition.handler(args)`` is exactly what the CLI ends up doing."""


def _text(body: str, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        result["is_error"] = True
    return result


def build_toolset(
    orchestrator: "TeamOrchestrator", *, member: TeamMember, task: TeamTask | None = None,
    producer=None, chat_id: str | None = None,
) -> StudioToolset | None:
    """Build the board + delegation + docs toolset for one member working on one task.

    Returns ``None`` when the SDK is not installed, and also when the user has switched the
    built-in server off on the MCP Servers page (§10.7) — the two are the same outcome for
    the run, and the system prompt already has a branch for a member with no Studio tools.
    Tools are rebuilt per run so their closures carry the acting member and the parent task
    — the identity every call is then checked against.

    ``task`` is ``None`` for a chat with the member (``teams/chat.py``): the same tools with
    the same routing, but no card of its own. A task the member creates from a chat is then
    a root of delegation, as one the user creates is, and the service lines a task run
    writes into its own transcript (``delegate``, ``board``, ``doc``) have nowhere to go —
    the chat shows the tool call itself, which says the same thing.
    """
    if SERVER_NAME in orchestrator.config.mcp.disabled:
        log.info("Studio tools are disabled for this project — running %s without them", member.id)
        return None
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        return None

    db = orchestrator.db
    docs: DocStore = orchestrator.docs
    # Per-run budget, closed over so it counts this session only. The depth ceiling in
    # create_task bounds how far work spreads; this bounds one agent's fan-out within a
    # single turn, which depth alone would not catch.
    budget = {"remaining": orchestrator.config.teams.max_delegated_tasks_per_task}

    async def resolve(reference: str) -> TeamTask | None:
        """A task on *this member's board*, from an id, a display id, or a number."""
        return await orchestrator.find_task(reference, team_id=member.team_id)

    async def names_for(rows: list[TeamTask]) -> dict[str, str]:
        """Member names for the tasks in hand, so a listing reads as people not as ids."""
        wanted = {row.assignee_id for row in rows} | {
            row.created_by for row in rows if row.created_by
        }
        async with db.session() as session:
            found = [await session.get(TeamMember, entry) for entry in wanted]
        return {entry.id: entry.name for entry in found if entry is not None}

    def line(row: TeamTask, names: dict[str, str]) -> str:
        marks = [str(row.status), names.get(row.assignee_id, row.assignee_id)]
        if row.assignee_id == member.id:
            marks.append("you")
        if row.depends_on:
            marks.append(f"waits for {len(row.depends_on)}")
        if row.archived:
            marks.append("archived")
        return f"- {orchestrator.display_id(row)} — {row.title} [{', '.join(marks)}]"

    @tool(
        "list_team",
        "List the team: who each member is, what they are responsible for, whether they "
        "are an AI agent or a human, and which of them you may assign work to.",
        {"type": "object", "properties": {}},
    )
    async def list_team(_args: dict[str, Any]) -> dict[str, Any]:
        async with db.session() as session:
            rows = list(
                (
                    await session.exec(
                        select(TeamMember).where(TeamMember.team_id == member.team_id)
                    )
                ).all()
            )
            open_counts: dict[str, int] = {}
            tasks = list(
                (
                    await session.exec(
                        select(TeamTask).where(TeamTask.team_id == member.team_id)
                    )
                ).all()
            )
        for entry in tasks:
            if entry.status not in (TaskStatus.DONE, TaskStatus.CANCELLED):
                open_counts[entry.assignee_id] = open_counts.get(entry.assignee_id, 0) + 1

        rows.sort(key=lambda m: (m.sort_order, m.name))
        by_id = {m.id: m for m in rows}
        lines: list[str] = []
        assignable: list[str] = []
        for entry in rows:
            manager = by_id.get(entry.manager_id or "")
            marks = [entry.kind]
            if entry.id == member.id:
                marks.append("you")
            if manager is not None:
                marks.append(f"reports to {manager.name}")
            open_now = open_counts.get(entry.id, 0)
            if open_now:
                marks.append(f"{open_now} open task(s)")
            lines.append(
                f"- {entry.id} — {entry.name}, {entry.role_title} [{', '.join(marks)}]\n"
                f"  responsibility: {entry.responsibility or 'not stated'}"
            )
            if entry.id != member.id and _may_assign(member, entry):
                assignable.append(f"{entry.id} ({entry.name}, {entry.role_title})")

        body = "\n".join(lines) or "No members."
        if assignable:
            body += "\n\nYou may create tasks for: " + ", ".join(assignable)
        else:
            body += (
                "\n\nYou may not assign work to anyone. Describe what needs doing in your "
                "summary and your manager will route it."
            )
        return _text(body)

    @tool(
        "create_task",
        "Put a task on another member's board. Use this to decompose your work and hand "
        "the pieces to the people responsible for them. Call list_team first to see ids. "
        "The task lands in Todo. Team Autostart Tasks may release it automatically; "
        "otherwise a person releases it. Do not wait on an unreleased task.",
        {
            "type": "object",
            "properties": {
                "assignee_id": {"type": "string", "description": "Member id from list_team."},
                "title": {"type": "string", "description": "Short imperative title."},
                "brief": {
                    "type": "string",
                    **({"maxLength": PRODUCER_BRIEF_MAX_CHARS} if producer else {}),
                    "description": (
                        "The task's full instructions. This becomes the assignee's first "
                        "prompt, so include the acceptance criteria and link any document "
                        "you wrote by its path."
                    ),
                },
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact file paths this task will write, create or delete, including "
                        "both paths of a rename. Use paths relative to the managed project "
                        "root or absolute paths; no directories or globs. Never include "
                        "files only read for context. Omitted or empty means no path locks; "
                        "paths mentioned in the brief do not create locks (§10.5)."
                    ),
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tasks that must reach done first, by display id (VS-01) or by "
                        "task id. Recorded on the task and honoured once someone releases "
                        "it out of Todo."
                    ),
                },
            },
            "required": ["assignee_id", "title", "brief"],
        },
    )
    async def create_task(args: dict[str, Any]) -> dict[str, Any]:
        assignee_id = str(args.get("assignee_id") or "").strip()
        title = str(args.get("title") or "").strip()
        if not assignee_id or not title:
            return _text("assignee_id and title are required.", is_error=True)

        brief = str(args.get("brief") or "")
        if producer and len(brief) > PRODUCER_BRIEF_MAX_CHARS:
            return _text(
                f"Producer task descriptions must be at most {PRODUCER_BRIEF_MAX_CHARS} characters. "
                "Shorten the brief, keeping the goal and acceptance criteria, and retry.",
                is_error=True,
            )

        # The agent knows tasks by the id it was shown — `VS-01` — so a dependency arrives
        # in that form far more often than as `task_…`; the board stores the opaque one.
        depends_on: list[str] = []
        for item in args.get("depends_on") or []:
            if not str(item).strip():
                continue
            upstream = await resolve(str(item))
            if upstream is None:
                return _text(f"No task {str(item)!r} on this board.", is_error=True)
            depends_on.append(upstream.id)

        if budget["remaining"] <= 0:
            return _text(
                "You have created as many tasks as one run may. Finish what you have and "
                "put anything still outstanding in your summary.",
                is_error=True,
            )

        try:
            created = await orchestrator.create_task(
                team_id=member.team_id,
                assignee_id=assignee_id,
                title=title,
                brief=brief,
                write_paths=args.get("write_paths", []),
                created_by=member.id,
                depends_on=depends_on,
                auto_start=True,
                parent_task=task,
                producer_chat_id=chat_id if producer else None,
            )
        except Exception as exc:  # routing, delegation ceiling, unknown member…
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

        budget["remaining"] -= 1
        async with db.session() as session:
            assignee = await session.get(TeamMember, created.assignee_id)
        who = assignee.name if assignee else created.assignee_id
        if task is not None:
            await orchestrator.record_message(
                task.id,
                member.id,
                role="assistant",
                kind="delegate",
                text=f"Delegated {orchestrator.display_id(created)} to {who}: {title}",
                data={
                    "task_id": created.id,
                    "display_id": orchestrator.display_id(created),
                    "assignee_id": created.assignee_id,
                },
            )
        if producer:
            note = ("Draft held in Todo. Include it in propose_producer_plan: Careful requires the user's "
                    "plan approval; Independent validates and releases that plan automatically.")
        elif assignee is not None and assignee.kind == MemberKind.HUMAN:
            note = "It is waiting in Todo for them to pick up."
        elif created.status == TaskStatus.TODO:
            note = (
                "It is waiting in the Todo column — nothing runs until a person releases it "
                "to Pending, so do not wait for its result or assume it has been done."
            )
        else:
            note = (
                "Team Autostart Tasks released it to the agent queue. "
                f"Current status: {created.status}. Dependencies and Solo Mode still apply."
            )
        return _text(
            f"Created {orchestrator.display_id(created)} for {who}. {note} "
            "Refer to it by that id when you mention it to anyone."
        )

    @tool(
        "list_tasks",
        "Search the team's task board. Use it to see what you and your colleagues are "
        "working on, to find the task a brief refers to by its id, and to check whether "
        "something you are waiting on is done. Filters combine; with none you get the "
        "whole board, newest first.",
        {
            "type": "object",
            "properties": {
                "status": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [str(value) for value in TaskStatus],
                    },
                    "description": (
                        "Keep only these statuses. `todo` is created but not released, "
                        "`pending` is released and runnable, `needs_review` is finished "
                        "and waiting on a manager."
                    ),
                },
                "assignee_id": {
                    "type": "string",
                    "description": "Member id from list_team. Whose board to look at.",
                },
                "mine": {
                    "type": "boolean",
                    "description": "Shorthand for assignee_id = you. Ignored if assignee_id is given.",
                },
                "created_by": {
                    "type": "string",
                    "description": "Member id of whoever raised the task. Omit for tasks the user created.",
                },
                "depends_on": {
                    "type": "string",
                    "description": (
                        "A task id or display id (VS-01). Answers with the tasks waiting "
                        "*on* that one — what your work is holding up."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": "Case-insensitive text to look for in the title or the brief.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many to return, newest first. Default 30.",
                    "minimum": 1,
                    "maximum": 200,
                },
            },
        },
    )
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        raw_status = args.get("status") or []
        statuses: list[TaskStatus] = []
        for entry in raw_status:
            try:
                statuses.append(TaskStatus(str(entry)))
            except ValueError:
                return _text(
                    f"Unknown status {entry!r}. Use one of: "
                    + ", ".join(str(value) for value in TaskStatus),
                    is_error=True,
                )

        assignee_id = str(args.get("assignee_id") or "").strip()
        if not assignee_id and args.get("mine"):
            assignee_id = member.id

        rows = await orchestrator.query_tasks(
            team_id=member.team_id,
            statuses=statuses or None,
            assignee_id=assignee_id or None,
            created_by=str(args.get("created_by") or "").strip() or None,
            depends_on=str(args.get("depends_on") or "").strip() or None,
            search=str(args.get("search") or "").strip() or None,
            limit=max(1, min(int(args.get("limit") or 30), 200)),
        )
        if not rows:
            return _text("No tasks match that.")
        names = await names_for(rows)
        return _text("\n".join(line(row, names) for row in rows))

    @tool(
        "get_task",
        "Read one task in full: its brief, status, who owns it, what it waits for and the "
        "summary of its last run. Use it on the ids list_tasks gives you, and on any id a "
        "brief or a note mentions. The transcript is not included — this is the card, not "
        "the conversation.",
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Display id (VS-01), bare number, or the opaque task id.",
                }
            },
            "required": ["task"],
        },
    )
    async def get_task(args: dict[str, Any]) -> dict[str, Any]:
        found = await resolve(str(args.get("task") or ""))
        if found is None:
            return _text(
                f"No task {str(args.get('task') or '')!r} on this board. "
                "Call list_tasks to see the ids.",
                is_error=True,
            )
        names = await names_for([found])
        async with db.session() as session:
            waits = [await session.get(TeamTask, entry) for entry in found.depends_on or []]

        lines = [
            f"{orchestrator.display_id(found)} — {found.title}",
            f"status: {found.status}"
            + (f" ({found.result_type})" if found.result_type else ""),
            f"assignee: {names.get(found.assignee_id, found.assignee_id)}"
            + (" (you)" if found.assignee_id == member.id else ""),
            f"created by: {names.get(found.created_by or '', found.created_by or 'the user')}",
            f"auto-start on release: {'yes' if found.auto_start else 'no'}",
            "write paths: " + (", ".join(found.write_paths) or "(none; no path locks)"),
        ]
        if waits:
            lines.append(
                "waits for: "
                + ", ".join(
                    f"{orchestrator.display_id(entry)} ({entry.status})"
                    for entry in waits
                    if entry is not None
                )
            )
        lines.append(f"\nbrief:\n{found.brief or '(none)'}")
        if found.result_summary:
            lines.append(f"\nlast summary:\n{found.result_summary}")
        if found.review_comment:
            lines.append(f"\nlast review comment:\n{found.review_comment}")
        if found.error:
            lines.append(f"\nerror:\n{found.error}")
        lines.append(
            f"\n{found.num_turns} turn(s), {found.total_tokens} tokens, "
            f"${found.total_cost_usd:.3f}, opaque id {found.id}"
        )
        return _text("\n".join(lines))

    @tool(
        "update_task",
        "Edit a task on the board: retitle it, correct its brief or write paths, change what it waits "
        "for, or move it between columns. You may edit your own tasks and those of the "
        "people who report to you; the built-in Producer may edit any task on its team. "
        "In Producer Mode use review_producer_task for execution and review decisions. "
        "Send only the fields you are changing. The task's id "
        "(VS-01) is fixed and cannot be edited — it is what everyone else calls this work.",
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Display id (VS-01), bare number, or the opaque task id.",
                },
                "title": {"type": "string", "description": "Short imperative title."},
                "brief": {
                    "type": "string",
                    **({"maxLength": PRODUCER_BRIEF_MAX_CHARS} if producer else {}),
                    "description": "Replaces the whole brief — read the task first if you are amending it.",
                },
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Replace the exact file paths this task will write, create or delete, "
                        "including both paths of a rename. Paths are relative to the managed "
                        "project root or absolute; no directories or globs. Exclude read-only "
                        "references. Empty clears the write set and takes no path locks. "
                        "Cannot change while the task is running (§10.5)."
                    ),
                },
                "assignee_id": {
                    "type": "string",
                    "description": (
                        "Hand the task to someone else. Bound by the same routes as "
                        "create_task, so call list_team first."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": [str(value) for value in TaskStatus],
                    "description": (
                        "Move it between columns. You cannot mark your own task done or "
                        "cancelled — finishing your run sends it to your manager for review."
                    ),
                },
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replaces the list. Task ids or display ids on this board.",
                },
                "auto_start": {
                    "type": "boolean",
                    "description": "Whether releasing the task to Pending should run it straight away.",
                },
            },
            "required": ["task"],
        },
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        if producer and isinstance(args.get("brief"), str) and len(args["brief"]) > PRODUCER_BRIEF_MAX_CHARS:
            return _text(
                f"Producer task descriptions must be at most {PRODUCER_BRIEF_MAX_CHARS} characters. "
                "Shorten the brief, keeping the goal and acceptance criteria, and retry.",
                is_error=True,
            )
        found = await resolve(str(args.get("task") or ""))
        if found is None:
            return _text(
                f"No task {str(args.get('task') or '')!r} on this board.", is_error=True
            )

        if producer and args.get("status"):
            held = found.producer_chat_id and not found.producer_approved
            if not held or args["status"] not in ("todo", "cancelled"):
                return _text("Use review_producer_task for execution and review decisions. "
                             "Producer drafts require plan approval before starting.", is_error=True)

        fields: dict[str, Any] = {}
        for key in ("title", "brief", "assignee_id"):
            if isinstance(args.get(key), str) and args[key].strip():
                fields[key] = args[key].strip()
        if isinstance(args.get("auto_start"), bool):
            fields["auto_start"] = args["auto_start"]
        if "write_paths" in args:
            fields["write_paths"] = args["write_paths"]

        if args.get("status"):
            try:
                wanted = TaskStatus(str(args["status"]))
            except ValueError:
                return _text(f"Unknown status {args['status']!r}.", is_error=True)
            if found.assignee_id == member.id and wanted in _AGENT_FORBIDDEN_SELF_STATUSES:
                return _text(
                    f"You cannot move your own task to {wanted}. Finish the run and it goes "
                    "to your manager's review queue; closing it is their decision.",
                    is_error=True,
                )
            if found.assignee_id == member.id and wanted == TaskStatus.NEEDS_REVIEW:
                # The review queue is read by result type first (§10.4), and a status move
                # carries none: the way into it is the report, and the Studio does the move.
                return _text(
                    "Your own task goes to review when the run ends — call finish_task with "
                    "result_type and your summary instead of moving the status.",
                    is_error=True,
                )
            fields["status"] = wanted

        if args.get("depends_on") is not None:
            resolved: list[str] = []
            for entry in args["depends_on"] or []:
                upstream = await resolve(str(entry))
                if upstream is None:
                    return _text(f"No task {str(entry)!r} on this board.", is_error=True)
                resolved.append(upstream.id)
            fields["depends_on"] = resolved

        if not fields:
            return _text("Nothing to change — send at least one field.", is_error=True)

        try:
            if producer:
                await producer.check_update_cost(chat_id, found, fields)
            updated = await orchestrator.update_task(found.id, fields, actor_id=member.id)
        except Exception as exc:  # routing, validation, a task mid-run…
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

        if task is not None:
            await orchestrator.record_message(
                task.id,
                member.id,
                role="assistant",
                kind="board",
                text=f"Edited {orchestrator.display_id(updated)}: {', '.join(sorted(fields))}",
                data={"task_id": updated.id, "fields": sorted(fields)},
            )
        return _text(
            f"Updated {orchestrator.display_id(updated)} — now {updated.status}. "
            f"Changed: {', '.join(sorted(fields))}."
        )

    @tool(
        "add_task_note",
        "Write a note into a task's history. This is the durable record a person reads on "
        "the board, unlike this transcript — use it for a finding, a decision or a blocker "
        "worth keeping. You may only write on a task that is yours.",
        {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Display id (VS-01), bare number, or the opaque task id.",
                },
                "text": {"type": "string", "description": "The note."},
            },
            "required": ["task", "text"],
        },
    )
    async def add_task_note(args: dict[str, Any]) -> dict[str, Any]:
        found = await resolve(str(args.get("task") or ""))
        if found is None:
            return _text(
                f"No task {str(args.get('task') or '')!r} on this board.", is_error=True
            )
        try:
            await orchestrator.add_note(
                found.id, str(args.get("text") or ""), author_id=member.id
            )
        except Exception as exc:  # not yours to write on, or an empty note
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)
        return _text(f"Noted on {orchestrator.display_id(found)}.")

    @tool(
        "list_docs",
        "List the project's shared documents — specs, design notes, test plans — that the "
        "team writes for each other.",
        {"type": "object", "properties": {}},
    )
    async def list_docs(_args: dict[str, Any]) -> dict[str, Any]:
        try:
            entries = docs.list_docs()
        except OSError as exc:
            return _text(f"Could not list documents: {exc}", is_error=True)
        if not entries:
            return _text("No documents yet.")
        return _text(
            "\n".join(f"- {entry.path} — {entry.title}" for entry in entries)
        )

    @tool(
        "read_doc",
        "Read one of the project's shared documents by path, as listed by list_docs.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. features/dash.md"}},
            "required": ["path"],
        },
    )
    async def read_doc(args: dict[str, Any]) -> dict[str, Any]:
        try:
            doc = docs.read(str(args.get("path") or ""))
        except FileNotFoundError as exc:
            return _text(str(exc), is_error=True)
        except (ValueError, PermissionError) as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)
        return _text(str(doc.get("content") or ""))

    @tool(
        "write_doc",
        "Write or replace a shared project document (Markdown). This is how you leave a "
        "specification, design note or test plan for the rest of the team; the whole file "
        "is replaced, so read it first if you are amending one.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path under the docs folder, e.g. features/dash.md",
                },
                "content": {"type": "string", "description": "Full Markdown body."},
            },
            "required": ["path", "content"],
        },
    )
    async def write_doc(args: dict[str, Any]) -> dict[str, Any]:
        try:
            written = await docs.write(
                str(args.get("path") or ""),
                str(args.get("content") or ""),
                author=member.id,
            )
        except (ValueError, PermissionError, OSError) as exc:
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)

        if task is not None:
            await orchestrator.record_message(
                task.id,
                member.id,
                role="assistant",
                kind="doc",
                text=f"Wrote document {written['path']}",
                data={"path": written["path"], "title": written["title"]},
            )
        return _text(f"Wrote {written['path']}. Reference it by that path when you brief others.")

    @tool(
        "finish_task",
        "Report how your task ended, with the summary your reviewer will read. Required at "
        "the end of every run: the task goes to your manager's review queue when the run "
        "ends, and this is what tells them whether it is finished work (`done`), a question "
        "only they can answer (`question`), or work that could not be done (`error`). "
        "Judge only the current task's scope and acceptance criteria, not the whole feature. "
        "A completed task is `done` even if the feature still awaits OTHER tasks; mention "
        "them in the summary. Use `question` only for unresolved questions or unfinished "
        "work within the current task that needs the reviewer's input. "
        "Calling it again replaces the earlier report. It does not end the run or move the "
        "task — finish your reply after it.",
        {
            "type": "object",
            "properties": {
                "result_type": {
                    "type": "string",
                    "enum": [str(value) for value in TaskResultType],
                    "description": (
                        "`done` — the current task is complete and ready to approve, even "
                        "if other tasks are still needed to finish the feature. `question` — "
                        "unresolved questions or unfinished work in the current task need "
                        "the reviewer's input; say what. Waiting for other tasks alone is "
                        "not a reason to choose `question`. "
                        "`error` — the work could not be done; say why and what you tried."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "What you changed, what you are asking, or what went wrong, and "
                        "anything your reviewer should check. Name tasks by their id and "
                        "documents by their path."
                    ),
                },
            },
            "required": ["result_type", "summary"],
        },
    )
    async def finish_task(args: dict[str, Any]) -> dict[str, Any]:
        if task is None:
            return _text(
                "This is a chat, not a task — there is no card to report a result on.",
                is_error=True,
            )
        try:
            updated = await orchestrator.report_result(
                task.id,
                result_type=str(args.get("result_type") or ""),
                summary=str(args.get("summary") or ""),
                actor_id=member.id,
            )
        except Exception as exc:  # an unknown type, an empty summary, a closed task…
            return _text(f"{type(exc).__name__}: {exc}", is_error=True)
        return _text(
            f"Recorded {orchestrator.display_id(updated)} as {updated.result_type}. It goes "
            "to review when this run ends — you can stop now."
        )

    tools = [
        list_team,
        list_tasks,
        get_task,
        create_task,
        update_task,
        add_task_note,
        finish_task,
        list_docs,
        read_doc,
        write_doc,
    ]
    if orchestrator.authoring is not None:
        tools.extend(orchestrator.authoring.tools())
    if producer:
        tools.extend(producer.tools(chat_id))
        # One mutation at a time with Send/Approve/Stop, including parallel MCP calls.
        # Check the mode afresh: a toolset held by a stopped runtime loses its authority.
        def guarded(definition):
            async def handle(args):
                try:
                    async with producer.chats.lock(chat_id):
                        await producer._chat(chat_id)
                        changes_tasks = definition.name in {"create_task", "update_task", "review_producer_task"}
                        if changes_tasks:
                            await producer.require_checklist(chat_id)
                        result = await definition.handler(args)
                        if changes_tasks and not result.get("is_error"):
                            result["content"].append({"type": "text", "text":
                                "Update the session Plan with update_producer_checklist to reflect this change "
                                "before proposing a plan, waiting, asking the user or ending this turn."})
                        return result
                except Exception as exc:
                    return _text(f"{type(exc).__name__}: {exc}", is_error=True)
            return replace(definition, handler=handle)
        tools = [guarded(definition) for definition in tools]
    server = create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
    return StudioToolset(
        mcp_servers={SERVER_NAME: server},
        allowed_tools=[f"mcp__{SERVER_NAME}__{definition.name}" for definition in tools],
        definitions=tools,
    )


def _may_assign(actor: TeamMember, candidate: TeamMember) -> bool:
    """Mirror of ``check_can_assign`` used only to describe the roster to the agent."""
    if candidate.manager_id == actor.id or actor.manager_id == candidate.id:
        return True
    allowed = set(actor.can_assign_to or [])
    return "*" in allowed or candidate.id in allowed

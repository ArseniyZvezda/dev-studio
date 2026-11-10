"""Teams: org hierarchy, task board and agent transcripts (§10).

The SQLModel table for a unit of work is ``TeamTask`` rather than ``Task`` (spec §10.2)
purely to keep it unambiguous against ``asyncio.Task``, which this codebase uses heavily.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel

from .base import EnumString, UtcDateTime, new_id, utcnow


class TaskStatus(StrEnum):
    TODO = "todo"
    """Intake (§10.4). Released to Pending manually, by Start All, or team autostart."""

    PENDING = "pending"
    """Released and runnable. The only status a session may start from."""

    IN_PROGRESS = "in_progress"
    NEEDS_REVIEW = "needs_review"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.CANCELLED})


class TaskResultType(StrEnum):
    """How the work ended, said by whoever did it when the task goes to review (§10.4).

    Three answers and no more, because the reviewer reads the colour before the words:
    ``done`` (green) is work that is finished and asks for approval, ``question`` (blue)
    is work that stopped on something only the reviewer can decide, ``error`` (red) is
    work that could not be done and says why. An agent reports it with the ``finish_task``
    tool, a person with the Submit form; the status move itself is the Studio's.
    """

    DONE = "done"
    QUESTION = "question"
    ERROR = "error"

#: How many digits a task number is padded to when it is displayed (§10.2).
#:
#: **Two, as a minimum rather than a width.** ``VS-01`` … ``VS-99`` line up in a column and
#: sort as text the same way they sort as numbers, which is the whole reason to pad at all;
#: past that the number simply grows (``VS-100``), because widening the pad later would
#: rename every id a person has already written down. One digit was rejected for the same
#: reason — a board's first nine tasks would read differently from the rest of its life.
TASK_NUMBER_DIGITS = 2

#: ``VS-01``, ``vs 1``, ``VS_1`` or a bare ``1``. The code is captured and then ignored on
#: purpose: a project that renames its code still has to answer to the ids already written
#: into briefs and documents, and the number is what identifies the task.
_TASK_REFERENCE = re.compile(r"^\s*(?:[A-Za-z][A-Za-z0-9]*\s*[-_ ]\s*)?0*(\d+)\s*$")


def format_task_id(code: str, number: int) -> str:
    """The human-readable id of a task — ``VS-01`` (§10.2)."""
    return f"{code}-{number:0{TASK_NUMBER_DIGITS}d}"


def parse_task_number(reference: str) -> int | None:
    """The task number a human-readable reference names, or ``None`` if it names none."""
    match = _TASK_REFERENCE.match(reference or "")
    return int(match.group(1)) if match else None


class ProjectCounter(SQLModel, table=True):
    """A counter that must keep going up even when the rows it numbered are gone.

    One row, ``name="task_number"`` (§10.2). ``MAX(TeamTask.number) + 1`` on its own is not
    enough: deleting the newest task lowers the maximum, and the next task created would
    then be handed an id that is already written into a brief, a document or a commit
    message. A counter the delete cannot see is the difference between "the number nobody
    else has" and "the number nobody has *right now*".

    Seeded from that same maximum whenever it is behind, which is what makes it correct on
    a database created before this table existed and immediately after a backfill.
    """

    __tablename__ = "project_counter"

    name: str = Field(primary_key=True)
    value: int = 0


class Team(SQLModel, table=True):
    __tablename__ = "team"

    id: str = Field(default_factory=lambda: new_id("team"), primary_key=True)
    name: str
    description: str = ""
    autostart_tasks: bool = False
    """Release agent tasks arriving in Todo automatically (§10.4). Stored in team YAML."""
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class MemberKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class TeamMember(SQLModel, table=True):
    """A role in the org chart. ``manager_id`` self-FK forms the management vertical."""

    __tablename__ = "team_member"

    id: str = Field(default_factory=lambda: new_id("mbr"), primary_key=True)
    team_id: str = Field(foreign_key="team.id", index=True)
    name: str
    role_title: str
    kind: str = MemberKind.AGENT
    builtin: str | None = None
    """Reserved ``director`` (§10.2), ``producer`` or ``effort_solver`` (§10.11) role."""
    """``agent`` runs through an ``AgentBackend``; ``human`` gets an inbox instead (§10.2).

    A human member's tasks never start a session: the person claims the task, works
    outside the Studio, and submits it for review by hand.
    """

    manager_id: str | None = Field(default=None, index=True)
    responsibility: str = ""
    """Free text folded into the agent's system prompt."""

    can_assign_to: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Peers this member may hand work to on top of their own reports (§10.4).

    Member ids, or the single entry ``"*"`` for "anyone on the team". The vertical
    (reports, and the member's own manager) is always allowed without listing it.
    """

    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    agent_backend: str = "claude_code"
    agent: str | None = None
    """Run this member's tasks as an agent that already exists, by name (§10.3).

    Null — the default — means the Studio projects the member into one itself
    (``teams/agents.py``). Set, it names an agent the project or the machine already
    defines, and the Studio writes nothing: the roster then decides *who* the work is
    routed to while somebody else's definition decides *how* it is carried out.
    """

    model: str = "sonnet"
    effort: str | None = None
    """How hard the model works per turn — ``low`` … ``max``, or null for the model's own
    default. Nullable on purpose: "not chosen" and "chosen as low" are different states,
    and only the first should follow the model when it changes."""

    effort_solver_weights: dict[str, float] | None = Field(
        default=None, sa_column=Column(JSON, nullable=True, info={"migration_default": "NULL"}),
    )
    """Effort Solver's custom component weights (§10.11); null uses built-in defaults."""

    allowed_tools: list[str] = Field(
        default_factory=lambda: ["Read", "Grep", "Glob"], sa_column=Column(JSON)
    )
    skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Skills from ``.voxarium_studio/skills/`` this member may use, by folder name (§10.10).

    Empty by default, and that is the rule rather than a starting point: a skill in the project
    reaches no one until somebody gives it to a member here. The session is told each one's
    name, description and ``SKILL.md`` in its system prompt, task runs and chats alike
    (``teams/project_skills.py``). A name the folder no longer has is kept — a renamed or
    deleted skill is the user's to notice on the page, not something a sync should silently
    take off the roster — and is simply not offered to the session.
    """

    required_skills: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Skills activated at session start, in addition to shared Teams requirements (§10.10)."""
    required_documents: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Document paths whose full text accompanies the initial message (§10.10)."""

    run_settings_override: bool = False
    """Use this member's permission mode and turn limit instead of Team Settings (§10.3)."""

    max_turns: int = 40
    permission_mode: str = "bypassPermissions"
    isolate_worktree: bool = False
    """Run this member's tasks in a dedicated checkout when the VCS can provide one (§10.5).

    Off by default: tasks run in the project itself, and overlapping ``write_paths`` are
    serialised by coordination locks instead."""

    solo_mode: bool = False
    """One task at a time (§10.4).

    On, a released task waits in ``pending`` while any other task of this member is
    ``in_progress``, and starts by itself when that one ends — the member's released tasks
    are a queue, lowest number first. Off, every released task runs as soon as it is
    unblocked, up to the job engine's ``agent`` slots. Off by default; switch it on for a
    role whose parallel sessions would edit the same files against each other. Meaningless
    for a human, who claims tasks by hand and may hold several.
    """

    avatar_color: str | None = None
    avatar_icon: str | None = None
    """A Lucide icon name standing for what this member *does* — ``palette``, ``code``.

    Null means no icon: the member is drawn by their initials on the org chart and by a
    plain dot of their colour everywhere else, which is what every roster written before
    this field existed keeps doing. Stored as the bare name rather than as a discipline,
    because the catalogue is a frontend concern (``lib/member-icons.ts``) and a name this
    build does not know draws nothing rather than breaking the row — the same contract a
    folder's icon has.
    """

    pos_x: float | None = None
    pos_y: float | None = None
    """Where this node sits on the org chart, once someone has placed it by hand.

    Null on both means nobody has: the chart then derives a tidy-tree position instead, so
    a roster that was never rearranged carries no layout at all. Stored because the
    arrangement *is* an edit the user made — losing it on the next reload would make the
    canvas the one part of the team that does not persist.
    """

    sort_order: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=UtcDateTime)


class TeamTask(SQLModel, table=True):
    __tablename__ = "team_task"

    producer_chat_id: str | None = Field(default=None, index=True)
    producer_approved: bool = False
    """Producer drafts cannot run until their plan is approved (§10.11)."""

    id: str = Field(default_factory=lambda: new_id("task"), primary_key=True)
    number: int = Field(default=0, index=True, unique=True)
    """Sequential, per managed project, assigned on creation and never changed (§10.2).

    The primary key stays the opaque ``task_…`` string — it is a foreign key from
    ``agent_message`` and it travels in URLs and events — so this is an *additional*
    identity, the one a person can say out loud: with the project's code in front of it
    (:func:`format_task_id`) it reads as ``VS-01``.

    Immutable by construction rather than by a check: it is absent from
    ``TeamOrchestrator.EDITABLE_FIELDS``, from ``TaskUpdate`` and from every agent tool, so
    there is no route that could set it after the INSERT. Renumbering would silently
    invalidate every id already written into a brief, a document or a commit message.

    ``0`` means "not numbered yet" and exists only between a database gaining the column
    and ``TeamOrchestrator.ensure_task_numbers`` backfilling it, which happens on the next
    startup. The unique index is created after that backfill for exactly that reason.
    """

    team_id: str = Field(foreign_key="team.id", index=True)
    assignee_id: str = Field(foreign_key="team_member.id", index=True)
    created_by: str | None = None
    """Member id when delegated by a manager; ``None`` when created by the user."""

    delegation_depth: int = Field(default=0, index=True)
    """Hops from a task the user created. Bounded by ``config.teams.max_delegation_depth``
    so agents handing work to each other cannot spend indefinitely (§10.4)."""

    title: str
    brief: str = Field(default="", sa_column=Column(Text))
    write_paths: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Exact files this task will write, relative to the managed project or absolute (§10.5).

    Include files created or deleted and both paths of a rename. Reading or referencing
    a file never belongs here. An empty list takes no path locks; the brief is not used
    to infer a write set.
    """

    attachments: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: TaskStatus = Field(
        default=TaskStatus.TODO,
        sa_column=Column(EnumString(TaskStatus), index=True, nullable=False),
    )
    depends_on: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    auto_start: bool = True
    """Once the task is in ``pending``, start the session as soon as it is unblocked (§10.4).

    Read as "what happens when this task is released", never as "start it now": a task is
    created in ``todo`` and nothing runs from there, so this flag only takes effect from the
    moment the card is released to ``pending``. False keeps it parked there for an
    explicit Start instead. Start All and team autostart enable it when releasing work.
    Always False in effect for human assignees, who have no session.
    """

    pause_resume_pending: bool = False
    """A running task held by Producer Pause; Resume keeps its queue position (§10.11)."""

    resume_on_limit: bool = Field(default=True)
    """Wait out a spent usage limit rather than ending the run on it (§10.3).

    A run stopped by the limit records when the window refills (:attr:`limit_reset_at`),
    goes to ``blocked`` like any other stopped run, and starts itself again shortly after
    the reset — the same click a person would make, minus the part where they have to be
    awake for it.

    **On by default**, because a run the account stopped is one nobody chose to stop: the
    work is unfinished, the brief is unchanged, and the only thing wrong is a window that
    refills by itself. Waiting costs time and not money — the resumed session is the one
    that would otherwise have been started by hand — so the default that leaves a board
    stalled until somebody notices is the more expensive one.

    Kept on the row rather than in a setting, for the reason ``auto_start`` is: which cards
    are worth resuming unattended is a per-task judgement, and a project-wide switch would
    apply it to the one that is only half-briefed as well.
    """

    limit_reset_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    """When the usage limit that stopped this task refills, as the runtime reported it.

    Recorded whenever a limit is detected, **whether or not** :attr:`resume_on_limit` is
    on: it is the answer to "why is this blocked and when can it go again", which is worth
    showing either way, and it is what lets the flag be turned on afterwards and still know
    what to wait for. ``None`` on a limit the runtime could not date.
    """

    limit_resume_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    """When the wait ends and the task starts again — the reset plus a grace period.

    Stored rather than recomputed from :attr:`limit_reset_at` on demand, because it is the
    time a wait was actually armed for: ``teams.limit_resume_grace`` can be edited while a
    task is waiting, and the countdown on screen must not disagree with the timer behind
    it. Set only while a wait is armed, and cleared when the task moves on.
    """

    model: str | None = None
    """Run this one task on a model other than the assignee's (§10.3).

    Null — the default — means the member's, which is the answer for almost every card: a
    role is configured once for the work it usually does. What varies underneath it is the
    *task*, and the same member is right for a one-line fix and for a refactor while only
    the second is worth an expensive model. Kept on the row rather than chosen at the
    moment of clicking Start, so a resume, a continuation after a stall and a re-run after
    review all land on the model the first attempt used — a session that changed model
    halfway would be one transcript written by two of them.
    """

    effort: str | None = None
    """Effort for this one task, or null for the assignee's (§10.3).

    Null means "the member's setting", not "the model's default" — a task overriding a
    member's ``max`` down to nothing is not a wish anyone has, whereas overriding it to
    ``low`` is, and that is expressible. The member's own null keeps its meaning further
    down the chain.
    """

    agent_session_id: str | None = None
    """Claude Agent SDK session id, used to resume rather than restart (§10.3)."""

    remote_control_url: str | None = None
    """Where this session is reachable on claude.ai while Remote Control is on (§10.8).

    Set only while the run that opened the bridge is alive and cleared when it ends: the
    bridge dies with the CLI subprocess, so a value kept past that would be a link that
    looks live and controls nothing. The transcript entry keeps the URL as history.
    """

    worktree_path: str | None = None
    review_comment: str | None = Field(default=None, sa_column=Column(Text))
    result_summary: str | None = Field(default=None, sa_column=Column(Text))
    summary_locale: str | None = None
    """User's interface language for the task's short result (§10.4).

    Captured on create/release/start/request-changes, inherited by delegated tasks, and retained
    for unattended resumes. Null on older tasks asks for the user's conversation language.
    """

    result_type: str | None = None
    """A :class:`TaskResultType`, or null while nobody has said how the work ended (§10.4).

    Written beside the summary when the assignee reports — the ``finish_task`` tool, a
    person's Submit — and cleared when a run starts, so it always describes *this* arrival
    in review and never the last one. Null on a task in review means the session ended
    without reporting, which the card says in so many words rather than guessing.
    """

    error: str | None = Field(default=None, sa_column=Column(Text))
    total_cost_usd: float = 0.0
    num_turns: int = 0
    total_tokens: int = 0
    """All processed tokens, including repeated cached context (§10.4)."""
    total_uncached_tokens: int = 0
    """Input, cache writes and output; legacy unclassified usage stays in total only."""
    total_cached_tokens: int = 0
    """Repeated cache reads, already included in total_tokens."""
    total_input_tokens: int = 0
    """Known sent tokens, including cache creation but excluding cache reads (§10.4)."""
    total_output_tokens: int = 0
    commit_message: str | None = Field(default=None, sa_column=Column(Text))
    """The commit description drafted when the task last reached ``needs_review`` (§10.4).

    Written by a cheap model from the task's summary and diff, and rewritten on every
    arrival in review — a task sent back and resubmitted has new work to describe. The
    title is not stored: it is ``<display id> | <task title>`` and follows a rename.
    """

    commit_files: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    """Project-relative paths the draft covers — what the Commit button hands the backend.

    Captured with the description so the two describe the same state of the task; the
    changes column beside it is live and may already say more.
    """

    commit_generated_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    commit_error: str | None = Field(default=None, sa_column=Column(Text))
    """Why the last draft has no description — the model refused, the CLI is missing. Kept
    on the row rather than in the log because the section that shows the draft is where a
    reviewer looks for it."""

    commit_revision: str | None = None
    """What the Commit button last produced — sha, changeset — so the section can say the
    work is in version control rather than offering to commit it again."""

    archived: bool = Field(default=False, index=True)
    """Off the board, still on the books (§10.4).

    Verbose-only messages are discarded on archive (spec §10.4).

    Only a ``done`` task can be archived: the board's Done column fills up with finished
    work nobody needs to see again, and this is how it is put away. The row is kept whole
    — its cost, its Default transcript, its number — because the figure over the board and the
    Statistics page sum *every* finished task, and deleting one would quietly shrink what
    the project has paid. ``TeamOrchestrator.update_task`` enforces both halves of the
    invariant: archiving anything but a ``done`` task is refused, and a status move off
    ``done`` un-archives, so ``archived`` never describes a task the board has to show.
    """

    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    started_at: datetime | None = Field(default=None, sa_type=UtcDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UtcDateTime)


class AgentMessage(SQLModel, table=True):
    """Persisted transcript entry so a task's history survives restarts."""

    __tablename__ = "agent_message"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    task_id: str = Field(foreign_key="team_task.id", index=True)
    member_id: str = Field(index=True)
    role: str
    """``user`` | ``human`` | ``assistant`` | ``system`` | ``tool`` | ``result``.

    ``user`` is the Studio's operator, ``human`` a human team member writing in their own
    name — they are different voices in the transcript.
    """

    kind: str = "text"
    text: str = Field(default="", sa_column=Column(Text))
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)


class AgentChat(SQLModel, table=True):
    """A conversation between the user and one team member, outside the task board.

    A chat is the other way a member's session gets used: the same agent, the same
    tools and the same MCP servers a task run gets, but no brief, no board column and no
    review — what the member is told each turn is where the user *is* in the Studio right
    now (the page, the open document, the selected text) and what they typed. One chat is
    one Claude Agent SDK session, resumed turn after turn through
    :attr:`agent_session_id`, so the member remembers the conversation the way a task's
    session remembers its brief (§10.3).
    """

    __tablename__ = "agent_chat"

    producer_mode: bool = False
    producer_workspace: bool = False
    """Chats created on /producer keep the mode enabled until deletion (§10.11)."""
    producer_started: bool = False
    """A Producer turn has been requested; survives pauses and session compaction (§10.11)."""
    producer_paused: bool = False
    """Execution gate; the plan, wait and session remain intact while paused (§10.11)."""
    producer_policy: str = "careful"
    """Per-conversation decision policy; existing chats default to Careful (§10.11)."""
    producer_effort_mode: str = "agent"
    """Agent inherits the assignee's effort; Auto runs Effort Solver before creation (§10.11)."""
    estimate_task_cost_usd: float = 150.0
    """Estimated API cost ceiling per task, checked before Producer continuations (§10.11)."""
    producer_state: str = "off"
    producer_locale: str = "en"
    producer_plan: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    producer_plan_summary: str = ""
    producer_plan_version: int = 0
    producer_checklist: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    producer_checklist_revision: str = ""
    """Session Plan and the board revision it describes; survives approval (§10.11)."""
    producer_watch: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    producer_release_pending: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    producer_questions: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    producer_summary: str = Field(default="", sa_column=Column(Text))
    producer_compacted_through: str | None = None
    """Durable plan, waits and compacted context; independent of browser lifetime (§10.11)."""

    id: str = Field(default_factory=lambda: new_id("chat"), primary_key=True)
    member_id: str = Field(foreign_key="team_member.id", index=True)
    title: str = ""
    """Taken from the first message when nobody named the chat."""

    agent_session_id: str | None = None
    error: str | None = Field(default=None, sa_column=Column(Text))
    """What ended the last turn early, if anything did — cleared by the next turn."""

    total_cost_usd: float = 0.0
    num_turns: int = 0
    total_tokens: int = 0
    total_uncached_tokens: int = 0
    total_cached_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    last_message_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)
    """When anything was last said in it, by either side — what the chat list sorts by."""


class AgentChatMessage(SQLModel, table=True):
    """One transcript entry of a chat — the same shape as :class:`AgentMessage`.

    Its own table rather than a nullable ``task_id`` on ``agent_message``: that column is
    a real foreign key every task query joins on, and a chat is not a task with something
    missing. The columns match so the SPA renders both transcripts with one component.
    """

    __tablename__ = "agent_chat_message"

    id: str = Field(default_factory=lambda: new_id("cmsg"), primary_key=True)
    chat_id: str = Field(foreign_key="agent_chat.id", index=True)
    member_id: str = Field(index=True)
    role: str
    """``user`` | ``assistant`` | ``system`` | ``tool`` | ``result`` — as ``AgentMessage``."""

    kind: str = "text"
    text: str = Field(default="", sa_column=Column(Text))
    data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utcnow, index=True, sa_type=UtcDateTime)

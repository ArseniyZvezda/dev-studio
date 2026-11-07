"""Team composition on disk (``.voxarium_studio/teams/*.yaml``) synced into the database.

Who is on the team and who reports to whom is a decision worth committing to git, so it
lives in YAML alongside pipelines (§5). Task state and transcripts are runtime data and
stay in SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from io import StringIO
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select

from ..core.paths import ProjectPaths
from ..core.prompts import AgentPromptsConfig
from ..models.teams import Team, TeamMember, TeamTask
from .director_identity import DIRECTOR_ID, is_director
from .effort_solver import DEFAULT_SYSTEM_PROMPT, EFFORT_SOLVER_ID, EffortWeights, is_effort_solver
from .producer_identity import PRODUCER_ID, is_producer

log = logging.getLogger(__name__)


class MemberDefinition(BaseModel):
    id: str
    name: str
    role_title: str = ""
    kind: Literal["agent", "human"] = "agent"
    manager_id: str | None = None
    responsibility: str = ""
    can_assign_to: list[str] = Field(default_factory=list)
    """Peer ids this member may assign work to, or ``["*"]`` for anyone (§10.4)."""

    tags: list[str] = Field(default_factory=list)
    agent_backend: str = "claude_code"
    agent: str | None = None
    """An existing Claude Code agent to run as, instead of the one the Studio writes."""

    model: str = "sonnet"
    effort: str | None = None
    """``low`` … ``max``, or omitted for whatever the model does by default."""

    effort_solver_weights: EffortWeights | None = None

    allowed_tools: list[str] = Field(default_factory=lambda: ["Read", "Grep", "Glob"])
    skills: list[str] = Field(default_factory=list)
    """Folder names under ``.voxarium_studio/skills/`` this member may use (§10.10). Omitted when empty."""

    required_skills: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)

    @field_validator("skills", "required_skills", "required_documents", mode="before")
    @classmethod
    def _skills_as_written(cls, value: Any) -> Any:
        # Hand-written YAML: `skills:` left empty reads as null and a lone name as a string, and
        # either one failing validation would skip the whole team file rather than this key.
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return value

    run_settings_override: bool = False
    max_turns: int = 40
    permission_mode: str = "bypassPermissions"
    isolate_worktree: bool = True
    solo_mode: bool = True
    """Run one task at a time; a second released task waits for the first (§10.4)."""

    avatar_color: str | None = None
    avatar_icon: str | None = None
    """A Lucide icon name for the member's discipline — ``palette``, ``code`` (§10.2)."""

    pos_x: float | None = None
    pos_y: float | None = None
    """Hand-placed position on the org chart. Omitted entirely for a member nobody has
    moved, so a roster that was never rearranged stays free of layout keys."""


class TeamDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    autostart_tasks: bool = False
    members: list[MemberDefinition] = Field(default_factory=list)


def _member_key(team_id: str, member_id: str) -> str:
    """Namespace member ids by team so two teams can both have a 'lead'."""
    return f"{team_id}__{member_id}"


def _assign_keys(team_id: str, entries: list[str]) -> list[str]:
    """Namespace ``can_assign_to`` entries, leaving the ``*`` wildcard untouched."""
    return [entry if entry == "*" else _member_key(team_id, entry) for entry in entries]


class TeamStore:
    def __init__(
        self, paths: ProjectPaths, *, prompts: Callable[[], AgentPromptsConfig] = AgentPromptsConfig,
    ) -> None:
        self.paths = paths
        self._prompts = prompts

    def sync_producer_responsibility(self, member: TeamMember) -> bool:
        """Project the current Producer System Prompt into its read-only role (§10.11)."""
        if not is_producer(member):
            return False
        responsibility = self._prompts().text("producer_mode")
        if member.responsibility == responsibility:
            return False
        member.responsibility = responsibility
        return True

    @property
    def directory(self) -> Path:
        return self.paths.teams

    def load_definitions(self) -> list[TeamDefinition]:
        self.directory.mkdir(parents=True, exist_ok=True)
        definitions: list[TeamDefinition] = []
        for path in sorted(self.directory.glob("*.y*ml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                raw.setdefault("id", path.stem)
                raw.setdefault("name", path.stem.replace("-", " ").title())
                definitions.append(TeamDefinition.model_validate(raw))
            except Exception as exc:
                log.error("Skipping invalid team file %s: %s", path.name, exc)
        return definitions

    def file_for(self, team_id: str) -> Path:
        """The file a team was loaded from, or where a new one should go.

        A team's id and its filename are independent — the sample ships as
        ``default-team.yaml`` with ``id: team_default`` — so writing back to
        ``{team_id}.yaml`` would leave two files describing one team, the stale one still
        being loaded on the next sync. Find the file that actually declares this id.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.directory.glob("*.y*ml")):
            try:
                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            declared = raw.get("id") if isinstance(raw, dict) else None
            if (declared or path.stem) == team_id:
                return path
        return self.directory / f"{team_id}.yaml"

    async def sync_to_db(self, session) -> list[Team]:
        """Upsert YAML roles; the Producer's responsibility comes from Team Settings (§10.11)."""
        teams: list[Team] = []
        for definition in self.load_definitions():
            team = await session.get(Team, definition.id)
            if team is None:
                team = Team(id=definition.id, name=definition.name, description=definition.description)
            else:
                team.name = definition.name
                team.description = definition.description
            team.autostart_tasks = definition.autostart_tasks
            session.add(team)
            teams.append(team)

            declared_ids: set[str] = set()
            for order, member_def in enumerate(definition.members):
                key = _member_key(definition.id, member_def.id)
                declared_ids.add(key)
                manager_key = (
                    _member_key(definition.id, member_def.manager_id)
                    if member_def.manager_id
                    else None
                )
                member = await session.get(TeamMember, key)
                if member is None:
                    member = TeamMember(id=key, team_id=definition.id, name=member_def.name,
                                        role_title=member_def.role_title)
                member.team_id = definition.id
                member.name = member_def.name
                member.role_title = member_def.role_title
                member.kind = member_def.kind
                member.builtin = (
                    "producer" if is_producer(member) else "director" if is_director(member)
                    else "effort_solver" if is_effort_solver(member) else None
                )
                if is_producer(member) or is_effort_solver(member):
                    member.kind = "agent"
                elif is_director(member):
                    member.kind = "human"
                member.manager_id = manager_key
                member.responsibility = member_def.responsibility
                if is_effort_solver(member) and not member.responsibility.strip():
                    member.responsibility = DEFAULT_SYSTEM_PROMPT
                self.sync_producer_responsibility(member)
                member.can_assign_to = _assign_keys(definition.id, member_def.can_assign_to)
                member.tags = list(member_def.tags)
                member.agent_backend = member_def.agent_backend
                member.agent = member_def.agent
                member.model = member_def.model
                member.effort = member_def.effort
                member.effort_solver_weights = (
                    member_def.effort_solver_weights.model_dump()
                    if is_effort_solver(member) and member_def.effort_solver_weights is not None else None
                )
                member.allowed_tools = list(member_def.allowed_tools)
                member.skills = list(member_def.skills)
                member.required_skills = list(member_def.required_skills)
                member.required_documents = list(member_def.required_documents)
                member.run_settings_override = member_def.run_settings_override
                member.max_turns = member_def.max_turns
                member.permission_mode = member_def.permission_mode
                member.isolate_worktree = member_def.isolate_worktree
                member.solo_mode = member_def.solo_mode
                member.avatar_color = member_def.avatar_color
                member.avatar_icon = member_def.avatar_icon
                member.pos_x = member_def.pos_x
                member.pos_y = member_def.pos_y
                member.sort_order = order
                session.add(member)

            # §10.2: the reserved Director survives omission and is always a person.
            director_id = _member_key(team.id, DIRECTOR_ID)
            missing_director = director_id not in declared_ids
            if missing_director:
                director = await session.get(TeamMember, director_id)
                if director is None:
                    director = TeamMember(
                        id=director_id, team_id=team.id, name="Director", role_title="Director",
                        builtin="director", kind="human", sort_order=-1,
                    )
                director.builtin = "director"
                director.kind = "human"
                session.add(director)
                declared_ids.add(director_id)

            # §10.11: omission from hand-edited YAML never deletes the built-in role or
            # resets its saved settings. Write it back so the roster stays portable.
            producer_id = _member_key(team.id, PRODUCER_ID)
            missing_producer = producer_id not in declared_ids
            if missing_producer:
                producer = await session.get(TeamMember, producer_id)
                if producer is None:
                    producer = TeamMember(
                        id=producer_id, team_id=team.id, name="Producer", role_title="Producer",
                        builtin="producer", can_assign_to=["*"], sort_order=len(declared_ids),
                    )
                producer.builtin = "producer"
                producer.kind = "agent"
                self.sync_producer_responsibility(producer)
                session.add(producer)
                declared_ids.add(producer_id)

            # §10.11: the scorer is a permanent agent; a hand-edited roster cannot
            # remove it, and restoring an omitted row retains its custom settings.
            solver_id = _member_key(team.id, EFFORT_SOLVER_ID)
            missing_solver = solver_id not in declared_ids
            if missing_solver:
                solver = await session.get(TeamMember, solver_id)
                if solver is None:
                    solver = TeamMember(
                        id=solver_id, team_id=team.id, name="Effort Solver", role_title="Effort Solver",
                        builtin="effort_solver", responsibility=DEFAULT_SYSTEM_PROMPT,
                        allowed_tools=[], isolate_worktree=False,
                        avatar_color="#a855f7", sort_order=len(declared_ids),
                    )
                solver.builtin = "effort_solver"
                solver.kind = "agent"
                if not solver.responsibility.strip():
                    solver.responsibility = DEFAULT_SYSTEM_PROMPT
                session.add(solver)
                declared_ids.add(solver_id)

            # Members deleted from YAML are removed — but only while nothing points at
            # them. A member with tasks is kept: their board is history the user can still
            # read, and `team_task.assignee_id` is a real foreign key, so deleting them
            # would fail the transaction and take the whole sync down with it.
            result = await session.exec(
                select(TeamMember).where(TeamMember.team_id == definition.id)
            )
            for existing in result.all():
                if existing.id in declared_ids:
                    continue
                owned = await session.exec(
                    select(TeamTask.id).where(TeamTask.assignee_id == existing.id).limit(1)
                )
                if owned.first() is not None:
                    log.info(
                        "Member %s is gone from %s.yaml but still owns tasks — keeping the row",
                        existing.id,
                        definition.id,
                    )
                    continue
                log.info("Member %s removed from %s.yaml", existing.id, definition.id)
                await session.delete(existing)

            if missing_producer or missing_director or missing_solver:
                await session.flush()
                # Append only the built-in entry: materialising defaults on every other
                # member would rewrite an untouched roster and its explanatory comments.
                path = self.file_for(team.id)
                from ruamel.yaml import YAML
                round_trip = YAML()
                round_trip.preserve_quotes = True
                round_trip.indent(mapping=2, sequence=4, offset=2)
                payload = round_trip.load(path.read_text(encoding="utf-8")) or {}
                if payload.get("members") is None:
                    payload["members"] = []
                restored = (
                    ([director] if missing_director else [])
                    + ([producer] if missing_producer else [])
                    + ([solver] if missing_solver else [])
                )
                for builtin in restored:
                    values = {key: getattr(builtin, key) for key in MemberDefinition.model_fields}
                    prefix = f"{team.id}__"
                    values["id"] = builtin.id.removeprefix(prefix)
                    values["manager_id"] = (builtin.manager_id.removeprefix(prefix)
                                             if builtin.manager_id else None)
                    values["can_assign_to"] = [key.removeprefix(prefix) for key in builtin.can_assign_to]
                    entry = MemberDefinition(**values).model_dump(exclude_defaults=True)
                    if is_producer(builtin):
                        # §10.11: Settings owns this template; never freeze a copy in the roster.
                        entry.pop("responsibility", None)
                    payload["members"].append(entry)
                stream = StringIO()
                round_trip.dump(payload, stream)
                text = stream.getvalue()
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(text, encoding="utf-8")
                tmp.replace(path)

        return teams

    async def save_from_db(self, session, team_id: str) -> Path:
        """Write the current DB state of a team back to YAML after a UI edit."""
        team = await session.get(Team, team_id)
        if team is None:
            raise KeyError(team_id)
        result = await session.exec(
            select(TeamMember).where(TeamMember.team_id == team_id)
        )
        members = sorted(result.all(), key=lambda m: (m.sort_order, m.name))

        prefix = f"{team_id}__"

        def short(value: str | None) -> str | None:
            if value is None:
                return None
            return value[len(prefix):] if value.startswith(prefix) else value

        def serialise(member: TeamMember) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "id": short(member.id),
                "name": member.name,
                "role_title": member.role_title,
                "kind": member.kind,
                "manager_id": short(member.manager_id),
                "responsibility": member.responsibility,
                "can_assign_to": [
                    entry if entry == "*" else short(entry) for entry in member.can_assign_to
                ],
                "tags": member.tags,
                "agent_backend": member.agent_backend,
                "model": member.model,
                "allowed_tools": member.allowed_tools,
                "run_settings_override": member.run_settings_override,
                "max_turns": member.max_turns,
                "permission_mode": member.permission_mode,
                "isolate_worktree": member.isolate_worktree,
                "solo_mode": member.solo_mode,
                "avatar_color": member.avatar_color,
            }
            if is_producer(member):
                # Also removes obsolete responsibility overrides on the next roster edit.
                entry.pop("responsibility")
            # An icon, effort, skills, a borrowed agent and layout are written only once
            # somebody chooses them, rather than as nulls on every row of a file whose job is
            # partly to be read. `_dump_preserving` drops keys the payload omits, so clearing
            # any of them takes it back out of the file too — which is how "Tidy up" removes
            # coordinates and how effort goes back to meaning "whatever the model does by
            # default".
            if member.skills:
                entry["skills"] = list(member.skills)
            if member.required_skills:
                entry["required_skills"] = list(member.required_skills)
            if member.required_documents:
                entry["required_documents"] = list(member.required_documents)
            if member.avatar_icon:
                entry["avatar_icon"] = member.avatar_icon
            if member.effort:
                entry["effort"] = member.effort
            if is_effort_solver(member) and member.effort_solver_weights is not None:
                entry["effort_solver_weights"] = EffortWeights.model_validate(member.effort_solver_weights).model_dump()
            if member.agent:
                entry["agent"] = member.agent
            if member.pos_x is not None and member.pos_y is not None:
                entry["pos_x"] = member.pos_x
                entry["pos_y"] = member.pos_y
            return entry

        payload: dict[str, Any] = {
            "id": team.id,
            "name": team.name,
            "description": team.description,
            "autostart_tasks": team.autostart_tasks,
            "members": [serialise(member) for member in members],
        }
        path = self.file_for(team_id)
        text = _dump_preserving(path, payload)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path


def _dump_preserving(path: Path, payload: dict[str, Any]) -> str:
    """Serialise a team, keeping the comments and formatting of the file being replaced.

    The team roster is meant to be read and committed (§5), and the one `voxarium_studio init`
    ships is half explanation — what ``kind: human`` means, what a sideways route is for.
    A plain ``safe_dump`` erases all of it the first time anyone touches the member editor,
    which is a poor trade for a file whose job is partly to teach the format.

    ruamel's round-trip loader keeps comments attached to the keys they document, so this
    merges the new values into the existing document instead of rewriting it. Members that
    were added get appended; members that were removed drop out with their comments.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:  # pragma: no cover - optional; falls back to a clean rewrite
        return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, indent=2)

    round_trip = YAML()
    round_trip.preserve_quotes = True
    round_trip.indent(mapping=2, sequence=4, offset=2)
    round_trip.width = 100

    existing: Any = None
    if path.exists():
        try:
            existing = round_trip.load(path.read_text(encoding="utf-8"))
        except Exception as exc:  # a hand-edited file that no longer parses
            log.warning("Could not round-trip %s (%s); rewriting it clean", path.name, exc)
            existing = None

    if not isinstance(existing, dict):
        stream = StringIO()
        round_trip.dump(payload, stream)
        return stream.getvalue()

    for key, value in payload.items():
        if key == "members":
            continue
        existing[key] = value

    by_id = {}
    for entry in existing.get("members") or []:
        if isinstance(entry, dict) and entry.get("id") is not None:
            by_id[str(entry["id"])] = entry

    merged = []
    for member in payload["members"]:
        target = by_id.get(str(member["id"]))
        if target is None:
            merged.append(member)
            continue
        for key, value in member.items():
            target[key] = value
        # Keys the model no longer emits are stale; drop them rather than leave a lie.
        for stale in [k for k in target if k not in member]:
            del target[stale]
        merged.append(target)
    existing["members"] = merged

    stream = StringIO()
    round_trip.dump(existing, stream)
    return stream.getvalue()

"""Skills the project keeps for its agents: ``voxarium_studio/skills/<name>/SKILL.md`` (§10.10).

A skill is a folder: a ``SKILL.md`` — YAML frontmatter with a ``description``, then the
instructions, the Agent Skills format Claude Code itself reads — and whatever files those
instructions refer to. Its **name is the folder's name**. That is what the member editor
offers, what a member's ``skills`` list in the team's YAML stores and what the prompt calls it;
a ``name`` in the frontmatter is not consulted, so a skill copied in from somewhere else is
named by where it was put.

The same catalog includes read-only package sources: ``voxarium_studio:studio`` and the skill
folders declared by loaded plugins. These are available to every session without roster
assignment, and keep their canonical package paths rather than being copied into a project.
The namespaced Studio id is a public id only, never a filesystem component. Installed
agent skills join this catalog as read-only entries with stable opaque ids, an origin and
backend metadata. They remain opt-in; assigning one resolves its original installation
path through ``installed_skills.py`` without copying or modifying the installation.

**Project skills are not connected by default.** The folder is not ``.claude/skills/``, so neither the
developer's terminal nor an agent session discovers a skill in it. A member is given skills on
the Teams page (``TeamMember.skills``), and only that member's sessions — task runs and chats
alike — are told about them.

**Told by the system prompt, not loaded as Claude Code skills**, and that is the decision.
Claude Code takes a skill from outside ``.claude/skills/`` only as part of a plugin, which is
how the Studio's own ``voxarium_studio:studio`` arrives (``teams/skill.py``). A plugin's skill paths may
not leave its root, though — measured on the bundled CLI 2.1.259 with an initialize-only
session (``.scratch/skills-probe``): a manifest naming ``./skills-extra/probe-inside`` loaded it,
one naming ``../outside/probe-escape`` did not — so handing one member two of the project's
skills would mean copying those folders into a plugin of that member's own on every run. The
SDK's ``skills`` allowlist is no way round it either: it filters every skill a session can see,
the user's own ones included. So the ``member_skills`` fragment (``core/prompts.py``) lists each
skill's name, description and ``SKILL.md`` path, and the session reads the file when its work
calls for it.

**One Read rule per skill given.** A session in an isolated worktree starts outside
``voxarium_studio/skills/``, where reading the main project is not allowed by default — the same
refusal the Studio skill's reference files met (``teams/skill.py``). Each rule is scoped to one
skill's folder, so being given a skill grants nothing beyond it.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..core.paths import ProjectPaths
from ..explorer.service import remove_path
from .docs import put_back, rename_directory
from .installed_skills import InstalledSkill, discover_installed_skills
from .skill import rule_path

log = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"

MAX_DESCRIPTION_CHARS = 1024
"""The Agent Skills format's own cap on a description, and what keeps one pasted essay from
becoming the longest part of every system prompt its member is sent."""

MAX_SKILL_BYTES = 1024 * 1024
"""The largest ``SKILL.md`` the page reads or writes. Instructions, not an asset store: anything
bigger belongs in a file beside it, which a session reads only when the skill says to."""

MAX_LISTED_FILES = 100
"""How many of a skill's files the listing names. The count goes on past it (to
:data:`MAX_COUNTED_FILES`), so a folder holding a script's dependencies still says so."""

MAX_COUNTED_FILES = 2000

MAX_NAME_CHARS = 128

MAX_NEW_NAME_CHARS = 64

#: What the Studio names a skill it creates: lower-case words joined by hyphens, which is what the
#: Agent Skills format asks of a skill's ``name``. A folder somebody copied in is read whatever it
#: is called (:func:`check_name`), so this binds only what the Studio itself writes.
_NEW_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: Everything Windows refuses in a folder name, and the separators everywhere.
_UNSAFE_IN_NAME = re.compile(r'[\x00-\x1f<>:"|?*/\\]')

_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)

_FRONTMATTER = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.DOTALL | re.MULTILINE
)

#: Folders a skill's file count does not descend into: version control and a script's installed
#: dependencies are not what anybody means by "the files of this skill".
_UNCOUNTED_DIRECTORIES = frozenset({"node_modules", "__pycache__", ".git", ".venv", "venv"})


class SkillChangedOnDisk(Exception):
    """``SKILL.md`` changed since the editor read it — refused rather than written over."""


def check_name(name: str) -> str:
    """A skill's name as a folder of ``voxarium_studio/skills/`` may carry it; raises ``ValueError``.

    Only the shape, never existence: this guards a member's ``skills`` list and the name in a
    URL, where the answer to "is it there" is the listing's, and a roster naming a skill this
    checkout does not have must still load.
    """
    # A packaged skill's public id is not used as a filesystem component.
    if name == "voxarium_studio:studio" or (
        isinstance(name, str) and re.fullmatch(r"installed:[0-9a-f]{32}", name)
    ):
        return name
    if not isinstance(name, str) or not name:
        raise ValueError("A skill needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"A skill's name is at most {MAX_NAME_CHARS} characters")
    if name != name.strip():
        raise ValueError(f"{name!r} starts or ends with a space, which a folder name cannot keep")
    if name in (".", "..") or name.startswith("."):
        raise ValueError(f"{name!r} is a hidden folder, not a skill")
    if _UNSAFE_IN_NAME.search(name):
        raise ValueError(f"{name!r} is a folder name, not a path — no slashes or <>:\"|?*")
    if name.endswith("."):
        # Windows drops a trailing dot without saying so, and the folder would not be what the
        # roster calls it.
        raise ValueError(f"{name!r} ends in a dot, which a folder name cannot keep")
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        raise ValueError(f"{name!r} is reserved by Windows for a device")
    return name


def check_new_name(name: str) -> str:
    """The name of a skill the Studio is about to create; raises ``ValueError``."""
    check_name(name)
    if len(name) > MAX_NEW_NAME_CHARS or not _NEW_NAME.fullmatch(name):
        raise ValueError(
            "A skill's name is lower-case words joined by hyphens, like 'level-prototyping'"
        )
    return name


def normalise_names(names: Iterable[str]) -> list[str]:
    """A member's skill list as it is stored: each name once, in the order first given."""
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def describe(text: str) -> tuple[str, str | None]:
    """The description a ``SKILL.md`` gives, and what is wrong with it if anything is.

    The description is folded onto one line — it is sent as one line of a list — and cut at
    :data:`MAX_DESCRIPTION_CHARS`. A problem does not make the skill unusable: the instructions
    are still there to be read, and only the line saying when to read them is weaker.
    """
    match = _FRONTMATTER.match(text)
    if match is None:
        return "", "No frontmatter, so agents see this skill's name and nothing about when to use it"
    try:
        front = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        where = getattr(exc, "problem_mark", None)
        line = f" (line {where.line + 2})" if where is not None else ""
        return "", f"The frontmatter does not parse{line}, so agents see only the name"
    if front is None:
        front = {}
    if not isinstance(front, dict):
        return "", "The frontmatter is not a set of key: value lines, so agents see only the name"

    raw = front.get("description")
    description = " ".join(str(raw).split()) if raw is not None else ""
    if not description:
        return "", "No description, so agents see this skill's name and nothing about when to use it"
    if len(description) > MAX_DESCRIPTION_CHARS:
        return (
            description[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…",
            f"The description is over {MAX_DESCRIPTION_CHARS} characters; agents are sent the "
            "start of it",
        )
    return description, None


def template(name: str, description: str) -> str:
    """The ``SKILL.md`` a new skill starts as — the format's frontmatter and a heading."""
    front = yaml.safe_dump(
        {"name": name, "description": " ".join(description.split())},
        sort_keys=False,
        allow_unicode=True,
        width=100_000,
    )
    title = name.replace("-", " ").capitalize()
    return (
        f"---\n{front}---\n\n# {title}\n\n"
        "Write what an agent should do when this skill applies, step by step. Put any reference "
        "files, templates or scripts it needs beside this file and name them by their path "
        "relative to this folder.\n"
    )


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    directory: Path
    description: str
    warning: str | None
    """Why agents are told less than they could be — no description, a frontmatter that does
    not parse. Shown on the Skills page; the skill is listed and can be given either way."""

    files: tuple[str, ...]
    """What is in the folder besides ``SKILL.md``, relative to it, up to :data:`MAX_LISTED_FILES`."""

    file_count: int
    updated_at: str
    """``SKILL.md``'s modification time — what a conditional write is checked against."""

    source: str | None = None
    """Read-only package/backend origin; None for a Studio-managed project skill."""

    display_name: str | None = None
    scope: str = "project"
    backends: tuple[str, ...] = ()
    readonly: bool = False
    automatic: bool = False

    @property
    def skill_file(self) -> Path:
        return self.directory / SKILL_FILE


@dataclass(frozen=True, slots=True)
class InvalidSkill:
    """A folder in ``voxarium_studio/skills/`` that is not a skill yet, and why."""

    name: str
    error: str


@dataclass(frozen=True, slots=True)
class MemberSkills:
    """The skills one member was given, as this checkout has them."""

    found: tuple[Skill, ...]
    missing: tuple[str, ...]
    """Names on the member that ``voxarium_studio/skills/`` has no skill for — a folder renamed or
    deleted after it was given. Told to nobody, and said in the task's transcript."""

    def allowed_tools(self) -> list[str]:
        return permission_rules(self.found)


_UNSPELLABLE_WARNED: set[Path] = set()


def permission_rules(skills: Iterable[Skill]) -> list[str]:
    """A ``Read`` rule for each skill's folder, in the form the CLI matches (``teams/skill.py``).

    A folder no rule can spell — a parenthesis or a comma in the project's path — gets none,
    said once per folder: a member with ``Read`` in its tools still opens the files, and one
    without loses only them.
    """
    rules: list[str] = []
    for skill in skills:
        where = rule_path(skill.directory)
        if where is None:
            if skill.directory not in _UNSPELLABLE_WARNED:
                _UNSPELLABLE_WARNED.add(skill.directory)
                log.warning(
                    "The skill folder %s cannot be written as a permission rule; members without "
                    "Read in their tools will be refused its files",
                    skill.directory,
                )
            continue
        rules.append(f"Read({where}/**)")
    return rules


def prompt_list(skills: Iterable[Skill]) -> str:
    """The ``{skills}`` lines of the ``member_skills`` fragment: name, what it is for, where."""
    lines = []
    for skill in skills:
        about = f": {skill.description}" if skill.description else ""
        lines.append(f"- {skill.display_name or skill.name}{about} (instructions: {skill.skill_file})")
    return "\n".join(lines)


class SkillStore:
    """Project folders, packages and installed agent skills (§10.10), read fresh on every call.

    Built-in public ids take precedence over project folders and never become paths under
    the project. The provider reads the current plugin registry, including after a reload.
    """

    def __init__(
        self, paths: ProjectPaths,
        *, builtins: Callable[[], dict[str, tuple[Path, str]]] | None = None,
        installed: Callable[[], dict[str, InstalledSkill]] | None = None,
    ) -> None:
        self.paths = paths
        self._builtins = builtins or (lambda: {})
        self._installed = installed or (lambda: discover_installed_skills(paths.root))

    def is_builtin(self, name: str) -> bool:
        return name in self._builtins()

    def _require_project_skill(self, name: str) -> None:
        if self.is_builtin(name):
            raise PermissionError(f"Built-in skill {name!r} is maintained by its package")
        if name.startswith("installed:"):
            raise PermissionError(f"Installed skill {name!r} is maintained outside Studio")

    def is_readonly(self, name: str) -> bool:
        return self.is_builtin(name) or name.startswith("installed:")

    @property
    def directory(self) -> Path:
        return self.paths.skills

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.paths.root).as_posix()
        except ValueError:
            return path.as_posix()

    # ------------------------------------------------------------------ reading

    def folder_names(self) -> list[str]:
        """Every visible folder here, sorted as a person would read the list."""
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return []
        names = []
        for entry in entries:
            if entry.name.startswith("."):
                # Hidden on purpose — which is also what keeps a skill halfway through being
                # deleted (``.<name>.deleting-…``) out of every list.
                continue
            try:
                if entry.is_dir():
                    names.append(entry.name)
            except OSError:
                continue
        return sorted(names, key=lambda name: (name.casefold(), name))

    def scan(self) -> tuple[list[Skill], list[InvalidSkill]]:
        """Every skill, and every folder that is not one yet."""
        skills: list[Skill] = []
        invalid: list[InvalidSkill] = []
        builtins = self._builtins()
        for name in self.folder_names():
            if name in builtins:
                continue
            try:
                check_name(name)
            except ValueError as exc:
                invalid.append(InvalidSkill(name=name, error=str(exc)))
                continue
            skill = self._read(self.directory / name)
            if skill is None:
                invalid.append(
                    InvalidSkill(
                        name=name,
                        error=f"No {SKILL_FILE} in this folder, so it is not a skill yet",
                    )
                )
            else:
                skills.append(skill)
        for name, (directory, source) in builtins.items():
            skill = self._read(directory, name=name, source=source, scope="builtin", automatic=True)
            if skill is not None:
                skills.append(skill)
        for entry in self._installed().values():
            skill = self._read_installed(entry)
            if skill is not None:
                skills.append(skill)
        skills.sort(key=lambda skill: ((skill.display_name or skill.name).casefold(), skill.name))
        return skills, invalid

    def get(self, name: str) -> Skill | None:
        """The skill called exactly *name*, or ``None``.

        Exactly, not as the filesystem would answer: on Windows ``Level-Design`` would open the
        folder ``level-design``, while on Linux it would not, and a roster that works on one
        checkout and not another is worse than a name that has to be picked from the list.
        """
        check_name(name)
        builtin = self._builtins().get(name)
        if builtin is not None:
            directory, source = builtin
            return self._read(directory, name=name, source=source, scope="builtin", automatic=True)
        if name.startswith("installed:"):
            entry = self._installed().get(name)
            return self._read_installed(entry) if entry else None
        if name not in self.folder_names():
            return None
        return self._read(self.directory / name)

    def for_member(self, names: Iterable[str]) -> MemberSkills:
        found: list[Skill] = []
        missing: list[str] = []
        names = normalise_names(names)
        installed = self._installed() if any(name.startswith("installed:") for name in names) else {}
        for name in names:
            skill = None
            try:
                if name.startswith("installed:"):
                    entry = installed.get(name)
                    skill = self._read_installed(entry) if entry else None
                else:
                    skill = self.get(name)
            except ValueError:
                skill = None
            if skill is None:
                missing.append(name)
            else:
                found.append(skill)
        return MemberSkills(found=tuple(found), missing=tuple(missing))

    def for_session(self, names: Iterable[str]) -> MemberSkills:
        """Built-ins are available to every session; project skills remain opt-in (§10.10)."""
        return self.for_member([*names, *self._builtins()])

    def read_text(self, name: str) -> tuple[Skill, str, bool]:
        """A skill with its ``SKILL.md`` for the editor: the text with ``\\n`` line endings, and
        whether the file on disk uses ``\\r\\n`` — which a write puts back (:meth:`write`)."""
        skill = self.get(name)
        if skill is None:
            raise FileNotFoundError(f"No skill {name!r} in {self.relative(self.directory)}")
        data = skill.skill_file.read_bytes()
        if len(data) > MAX_SKILL_BYTES:
            raise ValueError(
                f"{SKILL_FILE} is over {MAX_SKILL_BYTES // 1024} KiB — edit it in an editor"
            )
        text = data.decode("utf-8", errors="replace").removeprefix("\ufeff")
        return skill, text.replace("\r\n", "\n"), b"\r\n" in data

    def _read_installed(self, entry: InstalledSkill) -> Skill | None:
        return self._read(
            entry.directory, name=entry.name, source=entry.source, scope=entry.scope,
            backends=entry.backends, display_name=entry.display_name,
        )

    def _read(
        self, folder: Path, *, name: str | None = None, source: str | None = None,
        scope: str = "project", backends: tuple[str, ...] = (),
        display_name: str | None = None, automatic: bool = False,
    ) -> Skill | None:
        skill_file = folder / SKILL_FILE
        try:
            if not skill_file.is_file():
                return None
            stat = skill_file.stat()
            with skill_file.open("rb") as handle:
                head = handle.read(MAX_SKILL_BYTES)
        except OSError as exc:
            log.warning("Could not read %s: %s", skill_file, exc)
            return None
        description, warning = describe(head.decode("utf-8", errors="replace"))
        files, count = _files(folder)
        return Skill(
            name=name or folder.name,
            directory=folder,
            description=description,
            warning=warning,
            files=files,
            file_count=count,
            updated_at=_timestamp(stat.st_mtime),
            source=source,
            display_name=display_name or name or folder.name,
            scope=scope,
            backends=backends,
            readonly=source is not None,
            automatic=automatic,
        )

    # ------------------------------------------------------------------ writing

    def create(self, name: str, description: str = "", *, content: str | None = None) -> Skill:
        """A new folder with a ``SKILL.md`` in it. ``FileExistsError`` if the name is taken —
        asked of the filesystem, so on Windows a folder differing only in case takes it too."""
        check_new_name(name)
        if self.is_builtin(name):
            raise FileExistsError(f"Built-in skill {name!r} already exists")
        # MCP authors the complete skill in one write (§10.7), without a transient template.
        text = template(name, description) if content is None else content.replace("\r\n", "\n")
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError(f"{SKILL_FILE} may be at most {MAX_SKILL_BYTES // 1024} KiB")
        folder = self.directory / name
        if folder.exists():
            raise FileExistsError(f"{self.relative(folder)!r} already exists")
        self.directory.mkdir(parents=True, exist_ok=True)
        folder.mkdir()
        try:
            _write_atomic(folder / SKILL_FILE, text, newline="\n")
        except OSError:
            # A folder with no SKILL.md would sit on the page as "not a skill yet", under the
            # name the user is about to try again.
            remove_path(folder)
            raise
        skill = self._read(folder)
        assert skill is not None
        log.info("Skill %s created", name)
        return skill

    def write(self, name: str, content: str, *, expected_updated_at: str | None = None) -> Skill:
        """Replace a skill's ``SKILL.md``, keeping the line endings the file already had.

        ``expected_updated_at`` is the ``updated_at`` the editor read. Sent, a file that has
        changed since — an agent improving its own skill, an editor, a checkout — is refused with
        :class:`SkillChangedOnDisk` rather than written over. A folder with no ``SKILL.md`` yet
        may be given one.
        """
        self._require_project_skill(name)
        check_name(name)
        if name not in self.folder_names():
            raise FileNotFoundError(f"No skill {name!r} in {self.relative(self.directory)}")
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ValueError(f"{SKILL_FILE} may be at most {MAX_SKILL_BYTES // 1024} KiB")

        target = self.directory / name / SKILL_FILE
        crlf = False
        if target.is_file():
            if expected_updated_at is not None:
                current = _timestamp(target.stat().st_mtime)
                if current != expected_updated_at:
                    raise SkillChangedOnDisk(
                        f"{self.relative(target)!r} changed on disk at {current}; reload it "
                        "before saving"
                    )
            with target.open("rb") as handle:
                crlf = b"\r\n" in handle.read(MAX_SKILL_BYTES)
        elif expected_updated_at is not None:
            raise SkillChangedOnDisk(f"{self.relative(target)!r} was deleted on disk")

        text = content.replace("\r\n", "\n")
        _write_atomic(target, text, newline="\r\n" if crlf else "\n")
        skill = self._read(target.parent)
        assert skill is not None
        log.info("Skill %s: %s written", name, SKILL_FILE)
        return skill

    async def delete(self, name: str) -> None:
        """Delete a skill's folder with everything in it — a folder that is not a skill yet too.

        Moved aside, then removed, as a folder of documents is (``DocStore.delete_folder``, where
        the measurements are): ``rmtree`` on a folder something holds deletes what it reaches
        first and then fails, while a rename is refused whole — so a folder the system will not
        let go of loses nothing (``FolderInUse``), and a removal that still stops part-way puts
        what is left back under its name.
        """
        self._require_project_skill(name)
        check_name(name)
        if name not in self.folder_names():
            raise FileNotFoundError(f"No skill {name!r} in {self.relative(self.directory)}")
        folder = self.directory / name
        aside = folder.with_name(f".{name}.deleting-{secrets.token_hex(4)}")
        await rename_directory(folder, aside, doing="delete")
        try:
            remove_path(aside)
        except OSError as exc:
            where = "back where it was" if put_back(aside, folder) else f"in {aside.name!r}"
            raise OSError(
                f"Could not delete everything in {self.relative(folder)!r}: "
                f"{exc.strerror or exc}. What is left is {where}."
            ) from exc
        log.info("Skill %s deleted", name)


def _files(folder: Path) -> tuple[tuple[str, ...], int]:
    """What a skill carries besides ``SKILL.md``: names up to a limit, and how many there are.

    Breadth first, so when a folder has more than can be named the ones named are the files
    ``SKILL.md`` sits beside rather than whatever one deep subfolder happened to hold. The count
    stops at :data:`MAX_COUNTED_FILES`.
    """
    listed: list[str] = []
    count = 0
    pending = deque([folder])
    visited: set[Path] = set()
    while pending and count < MAX_COUNTED_FILES:
        current = pending.popleft()
        try:
            resolved = current.resolve()
            if resolved in visited:
                continue
            visited.add(resolved)
            entries = sorted(current.iterdir(), key=lambda entry: entry.name.casefold())
        except (OSError, RuntimeError):
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if entry.name not in _UNCOUNTED_DIRECTORIES and not entry.name.startswith("."):
                    pending.append(entry)
                continue
            relative = entry.relative_to(folder).as_posix()
            if relative == SKILL_FILE:
                continue
            count += 1
            if len(listed) < MAX_LISTED_FILES:
                listed.append(relative)
            if count >= MAX_COUNTED_FILES:
                break
    return tuple(listed), count


def _timestamp(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _write_atomic(path: Path, text: str, *, newline: str) -> None:
    """Written beside the target and moved onto it, so ``SKILL.md`` is never half-written — and
    the temporary file does not outlive a failed move to turn up in the next commit."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline=newline)
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise

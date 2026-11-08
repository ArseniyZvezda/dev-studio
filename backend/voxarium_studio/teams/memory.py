"""Project memory: short lasting facts every agent on the team sees and keeps (§10.12).

One Markdown file per entry in the managed project's ``voxarium_studio/memory``, named by the
entry: a YAML frontmatter with the one-line ``description`` every agent is shown and the
``author`` who wrote it last, then the fact itself. A directory rather than a table for the
reason documents are one (§10.4) — a person can read, diff and correct it in their own
editor — and a file per entry rather than one long document because two agents adding a
fact at the same moment must not race over one file.

Agents reach it through the Studio's tools (``recall``, ``remember``, ``forget``) and never
through their checkout, so a fact one of them records is there for everybody at once,
including a colleague working in an isolated worktree (§10.5). What every run is *sent* is
the index (:meth:`MemoryStore.index`) — one line per entry, capped — and never the bodies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..core.config import TeamsConfig
from ..core.paths import ProjectPaths
from ..ws.bus import EventBus
from ..ws.events import EventType
from .docs import _write_atomically
from .project_skills import _FRONTMATTER, _RESERVED_NAMES

log = logging.getLogger(__name__)

SUFFIX = ".md"

MAX_NAME_CHARS = 64
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

MAX_DESCRIPTION_CHARS = 300
"""The one line every run is sent per entry. Long enough for a fact and when it matters,
short enough that a hundred entries stay an index rather than a document."""

MAX_CONTENT_CHARS = 10_000
"""The body, read on demand. A fact that needs more than this is a document (§10.4)."""

SEARCH_LIMIT = 20

EMPTY_INDEX = "(no entries yet)"
"""What the prompt fragment's ``{entries}`` says when there is nothing to list — the
fragment still goes out, because it is also what tells a run to record what it learns."""


class MemoryChangedOnDisk(Exception):
    """The entry moved since the editor read it — an agent's ``remember`` landed meanwhile."""


def check_name(name: str) -> str:
    """The entry's id as given, or :class:`ValueError` saying what a name may be."""
    if not isinstance(name, str) or not name:
        raise ValueError("A memory entry needs a name")
    if len(name) > MAX_NAME_CHARS or not _NAME.fullmatch(name):
        raise ValueError(
            f"Invalid memory entry name {name!r}: use lowercase letters, digits and dashes, "
            f"starting with a letter or digit, at most {MAX_NAME_CHARS} characters"
        )
    if name in _RESERVED_NAMES:
        raise ValueError(f"{name!r} is a device name on Windows; choose another name")
    return name


def clean_description(description: str) -> str:
    """One line, as it is sent: whitespace folded, length checked."""
    folded = " ".join(str(description or "").split())
    if not folded:
        raise ValueError("A memory entry needs a one-line description")
    if len(folded) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"The description is {len(folded)} characters; keep it to {MAX_DESCRIPTION_CHARS}"
        )
    return folded


def clean_content(content: str) -> str:
    text = str(content or "").replace("\r\n", "\n").strip()
    if len(text) > MAX_CONTENT_CHARS:
        raise ValueError(
            f"The entry is {len(text)} characters; keep it to {MAX_CONTENT_CHARS}, or write a "
            "document and say in the entry where it is"
        )
    return text


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    name: str
    description: str
    author: str | None
    """The member who wrote it last; ``None`` is the user, or a file with no author."""
    updated_at: str
    content: str
    warning: str | None = None
    """Why agents are told less than they could be — a hand-written file without a
    description is still an entry, and its index line is only its name."""

    def index_line(self) -> str:
        if not self.description:
            return f"- {self.name}"
        # Cut here and not on reading, or the editor would save the cut text over the file.
        said = self.description
        if len(said) > MAX_DESCRIPTION_CHARS:
            said = said[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
        return f"- {self.name} — {said}"

    def to_json(self, *, content: bool = False) -> dict[str, object]:
        data: dict[str, object] = {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "updated_at": self.updated_at,
            "chars": len(self.content),
            "warning": self.warning,
        }
        if content:
            data["content"] = self.content
        return data


@dataclass(frozen=True, slots=True)
class InvalidEntry:
    """A file in the folder agents cannot be shown: named, so the page can say why."""

    file: str
    error: str

    def to_json(self) -> dict[str, str]:
        return {"file": self.file, "error": self.error}


@dataclass(frozen=True, slots=True)
class MemoryIndex:
    text: str
    """The lines a run is sent, and the omission note when the cap cut the list short."""
    total: int
    shown: int

    @property
    def truncated(self) -> bool:
        return self.shown < self.total

    def to_json(self) -> dict[str, object]:
        return {
            "text": self.text,
            "chars": len(self.text),
            "total": self.total,
            "shown": self.shown,
        }


def render_index(entries: list[MemoryEntry], max_chars: int | None = None) -> MemoryIndex:
    """One line per entry, in name order, as many as ``max_chars`` allows.

    Name order rather than recency: the index is part of every run's system prompt, and an
    order that reshuffled whenever an entry was touched would make every update look like a
    rewrite of the whole list. What the cap leaves out is said, with how to see it.
    """
    lines: list[str] = []
    used = 0
    for entry in entries:
        line = entry.index_line()
        cost = len(line) + (1 if lines else 0)
        if max_chars is not None and used + cost > max_chars:
            break
        lines.append(line)
        used += cost
    omitted = len(entries) - len(lines)
    if omitted:
        lines.append(
            f"… {omitted} more {'entry' if omitted == 1 else 'entries'} not shown here; "
            "call recall with no arguments to list them all."
        )
    return MemoryIndex(text="\n".join(lines), total=len(entries), shown=len(entries) - omitted)


def memory_prompt(store: "MemoryStore", settings: TeamsConfig, *, has_tools: bool) -> str | None:
    """The ``{entries}`` of the ``project_memory`` fragment, or ``None`` when none is sent.

    Nothing without the Studio's tools: the fragment tells a run to read entries with
    ``recall`` and keep them with ``remember``, and a run that cannot is better told nothing.
    A folder that cannot be read costs the run its memory, never the run.
    """
    if not settings.memory_enabled or not has_tools:
        return None
    try:
        index = store.index(settings.memory_index_chars)
    except OSError as exc:
        log.warning("Project memory unreadable, runs go without it: %s", exc)
        return None
    return index.text or EMPTY_INDEX


class MemoryStore:
    def __init__(self, paths: ProjectPaths, bus: EventBus | None = None) -> None:
        self.paths = paths
        self.bus = bus
        self._lock = asyncio.Lock()
        """Writes one at a time: ``remember`` decides "taken or not" and writes under it, so two
        agents creating one name are an error for the second rather than a silent overwrite."""

    @property
    def directory(self) -> Path:
        return self.paths.memory

    @property
    def relative_directory(self) -> str:
        return self.directory.relative_to(self.paths.root).as_posix()

    def path_for(self, name: str) -> Path:
        return self.directory / f"{check_name(name)}{SUFFIX}"

    # ------------------------------------------------------------------ reading

    def scan(self) -> tuple[list[MemoryEntry], list[InvalidEntry]]:
        """Every entry in name order, and the files that are not one."""
        if not self.directory.is_dir():
            return [], []
        entries: list[MemoryEntry] = []
        invalid: list[InvalidEntry] = []
        for path in sorted(self.directory.iterdir(), key=lambda item: item.name):
            if not path.is_file() or path.suffix.lower() != SUFFIX:
                continue
            try:
                check_name(path.stem)
                if path.suffix != SUFFIX:
                    raise ValueError(f"The extension must be {SUFFIX!r} in lowercase")
            except ValueError as exc:
                invalid.append(InvalidEntry(path.name, str(exc)))
                continue
            try:
                entries.append(self._read(path))
            except (OSError, UnicodeError, ValueError) as exc:
                invalid.append(InvalidEntry(path.name, str(exc)))
        return entries, invalid

    def entries(self) -> list[MemoryEntry]:
        return self.scan()[0]

    def get(self, name: str) -> MemoryEntry | None:
        path = self.path_for(name)
        if not path.is_file():
            return None
        try:
            return self._read(path)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Memory entry {name!r} cannot be read: {exc}") from exc

    def index(self, max_chars: int | None = None) -> MemoryIndex:
        return render_index(self.entries(), max_chars)

    def search(self, query: str, *, limit: int = SEARCH_LIMIT) -> list[MemoryEntry]:
        """Entries containing every word of ``query`` — name and description hits first."""
        terms = [term.casefold() for term in str(query or "").split()]
        if not terms:
            return []
        heading: list[MemoryEntry] = []
        body: list[MemoryEntry] = []
        for entry in self.entries():
            head = f"{entry.name} {entry.description}".casefold()
            whole = f"{head}\n{entry.content.casefold()}"
            if not all(term in whole for term in terms):
                continue
            (heading if all(term in head for term in terms) else body).append(entry)
        return [*heading, *body][:limit]

    def _read(self, path: Path) -> MemoryEntry:
        text = path.read_text(encoding="utf-8-sig")
        updated = _updated_at(path)
        match = _FRONTMATTER.match(text)
        if match is None:
            return MemoryEntry(
                name=path.stem, description="", author=None, updated_at=updated,
                content=text.strip(),
                warning="No frontmatter, so agents see this entry's name and nothing about it",
            )
        try:
            front = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            where = getattr(exc, "problem_mark", None)
            line = f" (line {where.line + 2})" if where is not None else ""
            raise ValueError(f"The frontmatter does not parse{line}") from exc
        if front is None:
            front = {}
        if not isinstance(front, dict):
            raise ValueError("The frontmatter is not a set of key: value lines")
        raw = front.get("description")
        description = " ".join(str(raw).split()) if raw is not None else ""
        warning = None
        if not description:
            warning = "No description, so agents see this entry's name and nothing about it"
        elif len(description) > MAX_DESCRIPTION_CHARS:
            warning = (
                f"The description is over {MAX_DESCRIPTION_CHARS} characters; agents are sent "
                "the start of it"
            )
        author = front.get("author")
        return MemoryEntry(
            name=path.stem,
            description=description,
            author=str(author) if author else None,
            updated_at=updated,
            content=text[match.end():].strip(),
            warning=warning,
        )

    # ------------------------------------------------------------------ writing

    async def write(
        self,
        name: str,
        *,
        description: str,
        content: str,
        author: str | None = None,
        create: bool = False,
        expected_updated_at: str | None = None,
    ) -> MemoryEntry:
        """Write one entry, atomically.

        ``create`` refuses with :class:`FileExistsError` when the name is taken — what an
        agent's ``remember`` sends unless it asked to replace, and what the page's New entry
        sends. ``expected_updated_at`` is the editor's: a file changed since it was read is
        refused with :class:`MemoryChangedOnDisk` rather than written over.
        """
        path = self.path_for(name)
        description = clean_description(description)
        body = clean_content(content)
        front: dict[str, str] = {"description": description}
        if author:
            front["author"] = author
        data = (
            "---\n"
            + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100_000)
            + "---\n\n"
            + (f"{body}\n" if body else "")
        )
        async with self._lock:
            exists = path.is_file()
            if create and exists:
                raise FileExistsError(f"Memory entry {name!r} already exists")
            if expected_updated_at is not None:
                if not exists:
                    raise FileNotFoundError(f"Memory entry {name!r} no longer exists")
                current = _updated_at(path)
                if current != expected_updated_at:
                    raise MemoryChangedOnDisk(
                        f"Memory entry {name!r} changed at {current}; reload it before saving"
                    )
            self.directory.mkdir(parents=True, exist_ok=True)
            # Its temporary `.md.tmp` name does not end in `.md`, so a scan does not list it.
            _write_atomically(path, data.encode("utf-8"))
            entry = self._read(path)
        await self._publish(name, author)
        log.info("Memory entry %s written by %s", name, author or "the user")
        return entry

    async def delete(self, name: str, *, author: str | None = None) -> None:
        path = self.path_for(name)
        async with self._lock:
            if not path.is_file():
                raise FileNotFoundError(f"No memory entry named {name!r}")
            path.unlink()
        await self._publish(name, author, deleted=True)
        log.info("Memory entry %s deleted by %s", name, author or "the user")

    async def _publish(self, name: str, author: str | None, *, deleted: bool = False) -> None:
        if self.bus is None:
            return
        payload: dict[str, object] = {"name": name, "author": author}
        if deleted:
            payload["deleted"] = True
        await self.bus.publish(EventType.TEAM_MEMORY_CHANGED, payload)


def _updated_at(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

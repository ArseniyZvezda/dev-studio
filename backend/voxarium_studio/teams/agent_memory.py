"""What the agent runtimes remember on their own, read so it can be imported (§10.12).

Each backend keeps a memory of its own, in the user's profile and in its own shape — which is
exactly why project memory is the Studio's (§10.12): nothing one runtime records there reaches
the other two. This module is the bridge in one direction. It *reads* those stores and turns
what it finds into candidates the Memory page offers; an import writes the chosen ones through
:class:`~voxarium_studio.teams.memory.MemoryStore` like any other entry. Nothing here ever
writes to a runtime's own files.

Where each runtime keeps it:

- **Claude Code** — ``<CLAUDE_CONFIG_DIR>/projects/<project>/memory/*.md``: a file per fact with
  ``name`` / ``description`` / ``type`` frontmatter, and ``MEMORY.md`` as their index.
- **Qwen Code** — the same layout under ``<qwen home>/projects/<project>/memory/`` (or
  ``.qwen/memory/`` in the project when it is told to keep memory local), plus what the older
  ``save_memory`` tool appended to ``QWEN.md`` under ``## Qwen Added Memories``.
- **Codex** — one handbook for every project, ``<CODEX_HOME>/memories/MEMORY.md``. Its top-level
  sections are the candidates; the derived files next to it (the summary, raw memories, rollout
  summaries) are stages of the same text and are not offered twice.

``<project>`` is the working directory with everything but letters and digits turned into
dashes. Claude Code keys it by the enclosing git repository when there is one, and Qwen
lower-cases it on Windows, so both the project root and its repository root are tried and
folder names are compared without case.

A new backend joins by adding a reader to :data:`READERS`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import installed_skills
from .memory import (
    MAX_CONTENT_CHARS,
    MAX_DESCRIPTION_CHARS,
    MAX_NAME_CHARS,
    MemoryStore,
)
from .project_skills import _FRONTMATTER, _RESERVED_NAMES

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 512 * 1024
"""A memory file larger than this is not a fact; it is skipped rather than read."""

MAX_CANDIDATES = 300
"""Per backend. Past it the page says the list was cut rather than growing without bound."""

INDEX_FILE = "MEMORY.md"
QWEN_ADDED = "qwen added memories"

PROJECT, GLOBAL = "project", "global"


@dataclass(slots=True)
class Candidate:
    """One thing a runtime remembers, as the entry it would become."""

    id: str
    """Stable across reads while the source does not change: what an import names it by."""
    backend: str
    name: str
    description: str
    content: str
    source: str
    """The file it was read from, with the home directory written as ``~``."""
    scope: str
    """``project`` — recorded for this project; ``global`` — the runtime's memory of everything."""
    kind: str | None = None
    """The runtime's own label for it (``feedback``, ``project``, ``user``…), when it has one."""
    related: bool = True
    """Whether a ``global`` one mentions this project. Always true for ``project``."""
    truncated: bool = False
    state: str = "new"
    """``new`` | ``same`` (an entry of that name already says this) | ``differs``."""

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "backend": self.backend,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "source": self.source,
            "scope": self.scope,
            "kind": self.kind,
            "related": self.related,
            "truncated": self.truncated,
            "state": self.state,
        }


@dataclass(slots=True)
class AgentMemory:
    """One backend's own memory, as far as it concerns this project."""

    backend: str
    locations: list[str] = field(default_factory=list)
    """Where it was looked for — shown when nothing was found, so "nothing" can be checked."""
    candidates: list[Candidate] = field(default_factory=list)
    truncated: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "locations": self.locations,
            "candidates": [item.to_json() for item in self.candidates],
            "truncated": self.truncated,
        }


# ---------------------------------------------------------------------- shaping text


def to_name(text: str) -> str:
    """``Build uses UE 5.4`` → ``build-uses-ue-5-4``; empty when nothing Latin is left."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:MAX_NAME_CHARS].rstrip("-")


def _clip_description(text: str) -> str:
    folded = " ".join(str(text or "").split())
    if len(folded) > MAX_DESCRIPTION_CHARS:
        folded = folded[: MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return folded


def _clip_content(text: str, source: str) -> tuple[str, bool]:
    body = str(text or "").replace("\r\n", "\n").strip()
    if len(body) <= MAX_CONTENT_CHARS:
        return body, False
    note = f"\n\n… (cut on import; the full text is in {source})"
    return body[: MAX_CONTENT_CHARS - len(note)].rstrip() + note, True


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = line.strip().lstrip("#>-* ").strip()
        if cleaned:
            return cleaned
    return ""


def _display(path: Path) -> str:
    home = installed_skills.user_home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return path.as_posix()


def _read_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        log.info("Agent memory file %s skipped: %s", path, exc)
        return None


# ---------------------------------------------------------------------- finding it


def _env_dir(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return Path(value).expanduser()
    return None


def claude_home() -> Path:
    return _env_dir("CLAUDE_CONFIG_DIR") or installed_skills.user_home() / ".claude"


def codex_home() -> Path:
    return _env_dir("CODEX_HOME") or installed_skills.user_home() / ".codex"


def qwen_home() -> Path:
    return _env_dir("QWEN_RUNTIME_DIR", "QWEN_HOME") or installed_skills.user_home() / ".qwen"


def _project_roots(project: Path) -> list[Path]:
    """The project, and the git repository around it when that is a different folder."""
    roots = [project]
    for folder in (project, *project.parents):
        try:
            if (folder / ".git").exists():
                if folder != project:
                    roots.append(folder)
                break
        except OSError:
            break
    return roots


def _slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(path)).casefold()


def _project_memory_dirs(home: Path, project: Path) -> list[Path]:
    """``<home>/projects/<project>/memory`` for every folder name that means this project."""
    wanted = {_slug(root) for root in _project_roots(project)}
    base = home / "projects"
    found: list[Path] = []
    try:
        for folder in sorted(base.iterdir(), key=lambda item: item.name):
            if folder.name.casefold() in wanted and (folder / "memory").is_dir():
                found.append(folder / "memory")
    except OSError:
        pass
    return found


# ---------------------------------------------------------------------- reading it


def _topic_files(directory: Path) -> Iterator[Path]:
    try:
        paths = sorted(directory.rglob("*.md"), key=lambda item: item.as_posix())
    except OSError:
        return
    for path in paths:
        if path.name != INDEX_FILE:
            yield path


def _topic_candidates(backend: str, directory: Path, scope: str = PROJECT) -> Iterator[Candidate]:
    """A folder of one-fact files with frontmatter — Claude Code's layout, and Qwen's."""
    for path in _topic_files(directory):
        text = _read_text(path)
        if text is None:
            continue
        front: dict[str, object] = {}
        body = text
        match = _FRONTMATTER.match(text)
        if match is not None:
            try:
                loaded = yaml.safe_load(match.group(1))
            except yaml.YAMLError:
                loaded = None
            if isinstance(loaded, dict):
                front, body = loaded, text[match.end():]
        if not body.strip() and not front.get("description"):
            continue
        source = _display(path)
        content, cut = _clip_content(body, source)
        metadata = front.get("metadata")
        kind = front.get("type") or (metadata.get("type") if isinstance(metadata, dict) else None)
        relative = path.relative_to(directory).as_posix()
        yield Candidate(
            id=f"{backend}:{directory.parent.name}/{relative}",
            backend=backend,
            name=to_name(str(front.get("name") or "")) or to_name(path.stem),
            description=_clip_description(str(front.get("description") or "")) or _clip_description(_first_line(body)),
            content=content,
            source=source,
            scope=scope,
            kind=str(kind) if kind else None,
            truncated=cut,
        )


_FENCE = re.compile(r"^\s*(```|~~~)")


def _sections(text: str, level: int) -> list[tuple[str, str]]:
    """``(heading, body)`` for every heading of ``level``, ignoring ones inside code fences."""
    marker = "#" * level + " "
    sections: list[tuple[str, list[str]]] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        if not fenced and line.startswith(marker):
            sections.append((line[len(marker):].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return [(title, "\n".join(lines).strip()) for title, lines in sections]


def _mentions(text: str, project: Path) -> bool:
    haystack = text.replace("\\", "/").casefold()
    return any(root.as_posix().casefold() in haystack for root in _project_roots(project))


def read_claude(project: Path) -> AgentMemory:
    home = claude_home()
    result = AgentMemory("claude_code", [_display(home / "projects" / "<project>" / "memory")])
    for directory in _project_memory_dirs(home, project):
        result.candidates.extend(_topic_candidates("claude_code", directory))
    return result


def read_codex(project: Path) -> AgentMemory:
    path = codex_home() / "memories" / INDEX_FILE
    result = AgentMemory("codex", [_display(path)])
    text = _read_text(path)
    if not text or not text.strip():
        return result
    source = _display(path)
    sections = _sections(text, 1) or _sections(text, 2) or [("Codex memory", text.strip())]
    seen: dict[str, int] = {}
    for title, body in sections:
        if not body:
            continue
        key = to_name(title) or "section"
        seen[key] = seen.get(key, 0) + 1
        content, cut = _clip_content(body, source)
        result.candidates.append(
            Candidate(
                id=f"codex:{INDEX_FILE}#{key}" + (f"~{seen[key]}" if seen[key] > 1 else ""),
                backend="codex",
                name=to_name(title),
                description=_clip_description(title),
                content=content,
                source=source,
                scope=GLOBAL,
                related=_mentions(f"{title}\n{body}", project),
                truncated=cut,
            )
        )
    return result


def _qwen_added(path: Path, scope: str) -> Iterator[Candidate]:
    """The bullets ``save_memory`` appended to a ``QWEN.md``, each one a fact."""
    text = _read_text(path)
    if not text:
        return
    source = _display(path) if scope == GLOBAL else path.name
    for title, body in _sections(text, 2):
        if title.casefold() != QWEN_ADDED:
            continue
        for chunk in re.split(r"(?m)^[-*] +", body)[1:]:
            fact = " ".join(chunk.split())
            if not fact:
                continue
            digest = hashlib.sha1(fact.encode("utf-8")).hexdigest()[:12]
            description = _clip_description(fact)
            yield Candidate(
                id=f"qwen:{scope}/QWEN.md#{digest}",
                backend="qwen",
                name=to_name(" ".join(fact.split()[:7])),
                description=description,
                content="" if description == fact else fact[:MAX_CONTENT_CHARS],
                source=source,
                scope=scope,
                related=scope == PROJECT,
            )


def read_qwen(project: Path) -> AgentMemory:
    home = qwen_home()
    local = project / ".qwen" / "memory"
    result = AgentMemory(
        "qwen",
        [_display(home / "projects" / "<project>" / "memory"), ".qwen/memory", "QWEN.md", _display(home / "QWEN.md")],
    )
    directories = _project_memory_dirs(home, project)
    if local.is_dir():
        directories.append(local)
    for directory in directories:
        result.candidates.extend(_topic_candidates("qwen", directory))
    result.candidates.extend(_qwen_added(project / "QWEN.md", PROJECT))
    result.candidates.extend(_qwen_added(home / "QWEN.md", GLOBAL))
    return result


READERS: dict[str, Callable[[Path], AgentMemory]] = {
    "claude_code": read_claude,
    "codex": read_codex,
    "qwen": read_qwen,
}
"""Backend name → reader, in the order the page lists them."""


# ---------------------------------------------------------------------- the page's view


def scan(project: Path, store: MemoryStore) -> list[AgentMemory]:
    """Every backend's own memory as candidates, named and compared against project memory.

    Names are settled here, across all backends at once: two runtimes that both remember
    ``build-commands`` would otherwise be one entry written twice, the second over the first.
    """
    existing = {entry.name: entry for entry in store.entries()}
    taken: set[str] = set()
    sources: list[AgentMemory] = []
    for backend, reader in READERS.items():
        try:
            source = reader(project)
        except OSError as exc:  # an unreadable profile costs that backend's list, not the page
            log.warning("Agent memory of %s unreadable: %s", backend, exc)
            source = AgentMemory(backend)
        if len(source.candidates) > MAX_CANDIDATES:
            source.candidates, source.truncated = source.candidates[:MAX_CANDIDATES], True
        for item in source.candidates:
            item.name = _free_name(item.name or f"{backend.replace('_', '-')}-memory", taken)
            if not item.description:
                item.description = item.name
            known = existing.get(item.name)
            if known is not None:
                same = known.description == item.description and known.content == item.content
                item.state = "same" if same else "differs"
        sources.append(source)
    return sources


def _free_name(base: str, taken: set[str]) -> str:
    if base in _RESERVED_NAMES:
        base = f"{base}-memory"
    name, number = base, 1
    while name in taken:
        number += 1
        suffix = f"-{number}"
        name = base[: MAX_NAME_CHARS - len(suffix)].rstrip("-") + suffix
    taken.add(name)
    return name


@dataclass(slots=True)
class ImportOutcome:
    imported: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {"imported": self.imported, "skipped": self.skipped}


async def import_candidates(
    project: Path, store: MemoryStore, wanted: dict[str, bool]
) -> ImportOutcome:
    """Write the candidates named by ``wanted`` (id → may replace an entry) into project memory.

    The sources are read again rather than taken from the request: what is written is what the
    runtime's file says now, never text a page sent. An entry that exists is replaced only when
    the request said so for that candidate — the same rule ``remember`` follows.
    """
    outcome = ImportOutcome()
    found = {item.id: item for source in scan(project, store) for item in source.candidates}
    for identity, replace in wanted.items():
        item = found.get(identity)
        if item is None:
            outcome.skipped.append({"id": identity, "name": "", "reason": "gone"})
            continue
        if item.state == "same":
            outcome.skipped.append({"id": identity, "name": item.name, "reason": "same"})
            continue
        try:
            await store.write(
                item.name, description=item.description, content=item.content, create=not replace
            )
        except FileExistsError:
            outcome.skipped.append({"id": identity, "name": item.name, "reason": "exists"})
        except (ValueError, OSError) as exc:
            outcome.skipped.append({"id": identity, "name": item.name, "reason": "error", "error": str(exc)})
        else:
            outcome.imported.append(item.name)
    return outcome

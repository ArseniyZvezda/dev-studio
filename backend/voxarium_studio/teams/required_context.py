"""Required startup skills and full Documents content, shared by task/chat sessions (§10.10)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from ..core.config import TeamsConfig
from ..models.teams import TeamMember
from . import skill as studio_skill
from .docs import DocStore
from .project_skills import MemberSkills, SkillStore, check_name, normalise_names


def checked_skills(store: SkillStore, names: Iterable[str], previous: Iterable[str] = ()) -> list[str]:
    """Validate additions, retaining unavailable existing choices so they can be removed."""
    wanted = normalise_names(names)
    previous = set(previous)
    for name in wanted:
        check_name(name)
        if name not in previous and store.get(name) is None:
            raise ValueError(f"Required skill {name!r} is unavailable")
    return wanted


def document_path(store: DocStore, name: str) -> str:
    # DocStore tolerates a leading slash for old document links; a configured requirement
    # must explicitly identify a file within Documents, never an absolute filesystem path.
    if PurePosixPath(name.replace("\\", "/")).is_absolute() or PureWindowsPath(name).drive:
        raise ValueError(f"Required document must be relative to Documents: {name!r}")
    return store.relative(store.resolve(name))


def checked_documents(store: DocStore, names: Iterable[str], previous: Iterable[str] = ()) -> list[str]:
    wanted = normalise_names(document_path(store, name) for name in names)
    old_paths = set()
    for name in previous:
        try:
            old_paths.add(document_path(store, name))
        except (ValueError, OSError):
            # A malformed hand-edited old value must not make clearing the list impossible.
            pass
    for name in wanted:
        if name not in old_paths and not store.resolve(name).is_file():
            raise ValueError(f"Required document {name!r} is unavailable")
    return wanted


@dataclass(frozen=True, slots=True)
class RequiredContext:
    skills: MemberSkills
    documents: tuple[str, ...]
    text: str

    def prepend(self, prompt: str) -> str:
        return f"{self.text}\n\n{prompt}" if self.text else prompt


def load_required_context(
    settings: TeamsConfig, member: TeamMember, skills: SkillStore, docs: DocStore,
    *, producer_mode: bool = False,
) -> RequiredContext:
    """Read each current requirement completely, or fail before a model is dispatched.

    Backend metadata describes discovery, not a restriction: a selected skill remains
    usable by its absolute path after switching backend (§10.10). The explicit startup
    instruction and inline body activate skills without depending on a CLI-specific tool.
    """
    names = normalise_names([*settings.required_skills, *(member.required_skills or [])])
    found = skills.for_member(names)
    if found.missing:
        raise ValueError("Required skills are unavailable: " + ", ".join(found.missing))
    documents = normalise_names(document_path(docs, name) for name in [
        *settings.required_documents, *(member.required_documents or []),
    ])
    # §10.3, §10.10–10.11: every session starts with Studio; active Producer chats
    # additionally start with Producer. Explicit selections must not duplicate either.
    studio_dir = studio_skill.BUNDLE / "skills" / studio_skill.SKILL_DIRECTORY
    startup_skills = [(studio_skill.SKILL_NAME, studio_dir, studio_dir / "SKILL.md")]
    automatic_names = {studio_skill.SKILL_NAME}
    if producer_mode:
        producer_dir = studio_skill.BUNDLE / "skills" / studio_skill.PRODUCER_SKILL_DIRECTORY
        startup_skills.append((studio_skill.PRODUCER_SKILL_NAME, producer_dir,
                               producer_dir / "SKILL.md"))
        automatic_names.add(studio_skill.PRODUCER_SKILL_NAME)
    startup_skills.extend(
        (skill.name, skill.directory, skill.skill_file)
        for skill in found.found if skill.name not in automatic_names
    )
    parts = [
        "Required startup skills\n"
        "Voxarium Studio automatically includes its own skill first. "
        + ("Active Producer chats also include the Producer skill. " if producer_mode else "")
        + "Other skills below were explicitly selected for this agent. "
        "Activate and apply every skill before beginning "
        "the assignment, in the listed order. Their full SKILL.md instructions are included; "
        "resolve referenced files relative to the stated skill directory."
    ]
    for name, directory, skill_file in startup_skills:
        try:
            body = skill_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read required skill {name!r}: {exc}") from exc
        if not body.strip():
            raise ValueError(f"Required skill {name!r} has empty instructions")
        parts.append(
            f"--- BEGIN REQUIRED SKILL: {name} ---\n"
            f"Skill directory: {directory}\nInstructions: {skill_file}\n\n{body}\n"
            f"--- END REQUIRED SKILL: {name} ---"
        )
    if documents:
        parts.append("Required documents\nThe following complete documents are part of the context for this assignment.")
    for name in documents:
        try:
            # Read directly after DocStore's path validation: no index, preview limit,
            # summarisation or newline rewriting may replace the full original content.
            with docs.resolve(name).open("r", encoding="utf-8-sig", newline="") as handle:
                body = handle.read()
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Cannot read required document {name!r}: {exc}") from exc
        parts.append(
            f"--- BEGIN REQUIRED DOCUMENT: {name} ---\n{body}\n"
            f"--- END REQUIRED DOCUMENT: {name} ---"
        )
    return RequiredContext(found, tuple(documents), "\n\n".join(parts))

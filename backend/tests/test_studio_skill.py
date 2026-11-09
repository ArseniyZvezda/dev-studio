"""The Studio skill: what teaches an agent the Studio runs to work inside it (§10.3).

``teams/skill.py`` ships a Claude Code plugin in the package and every session is handed it;
the system prompt only points at the skill. What is pinned here is what would otherwise rot
without anybody noticing: the bundle staying a plugin the CLI loads under the name the prompt
names, every tool the Studio exposes being named in it, its links reaching real files, and the
permission rules that let a session open it. Whether a *run* and a *chat* are handed it is
covered beside their prompts, in ``test_teams.py`` and ``test_chats.py``.

Nothing here reaches the real CLI. The measurements behind the rules — the invocation, and a
member without ``Read`` refused the reference files — are recorded in ``teams/skill.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams import skill as studio_skill
from voxarium_studio.teams.tools import build_toolset

SKILL_DIR = studio_skill.BUNDLE / "skills" / studio_skill.SKILL_DIRECTORY


def _frontmatter(text: str) -> dict:
    match = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md opens with a YAML frontmatter block"
    return yaml.safe_load(match.group(1))


def test_the_bundle_is_a_plugin_named_as_the_prompt_names_it() -> None:
    """``voxarium_studio:studio`` is the plugin's name and the skill's, joined — change either file and
    the prompt sends every member to a skill that does not exist."""
    manifest = json.loads(
        (studio_skill.BUNDLE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == studio_skill.PLUGIN_NAME

    front = _frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert front["name"] == studio_skill.SKILL_DIRECTORY
    assert studio_skill.SKILL_NAME == "voxarium_studio:studio"
    producer = _frontmatter((studio_skill.BUNDLE / "skills" / "producer" / "SKILL.md").read_text(
        encoding="utf-8"))
    assert producer["name"] == studio_skill.PRODUCER_SKILL_DIRECTORY
    assert studio_skill.PRODUCER_SKILL_NAME == "voxarium_studio:producer"
    assert 0 < len(producer["description"]) <= 1024
    # The description is what a session reads before it decides to open the skill, and the
    # skill format caps it.
    assert 0 < len(front["description"]) <= 1024


async def test_every_studio_tool_is_named_in_the_skill(ctx: AppContext) -> None:
    """The prompt no longer lists the tools, so a tool the skill does not name is a tool no
    member is told about. Add one to ``teams/tools.py`` and this fails until the skill says
    when to use it."""
    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember))).first()
    toolset = build_toolset(ctx.teams, member=member, task=None)
    if toolset is None:
        pytest.skip("claude-agent-sdk is not installed")

    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    missing = [d.name for d in toolset.definitions if f"`{d.name}`" not in text]
    assert not missing, f"SKILL.md does not name: {missing}"


def _prose(markdown: str) -> str:
    """Markdown without its code: examples of links are not links."""
    without_fences = re.sub(r"^```.*?^```[^\n]*$", "", markdown, flags=re.DOTALL | re.MULTILINE)
    return re.sub(r"(`+).+?\1", "", without_fences)


def test_every_link_in_the_skill_reaches_a_file() -> None:
    """A session opens a reference file by the link the skill gives it; a renamed file would
    leave the model reading a path that is not there, with a permission rule scoped to it."""
    files = sorted(SKILL_DIR.glob("*.md"))
    assert {path.name for path in files} >= {"SKILL.md", "documents.md", "project-files.md"}

    linked: set[str] = set()
    for path in files:
        for target in re.findall(r"\]\(([^)\s]+)\)", _prose(path.read_text(encoding="utf-8"))):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            assert (path.parent / target).is_file(), f"{path.name} links to missing {target}"
            linked.add(target)
    assert {"documents.md", "project-files.md"} <= linked, "every reference file is reachable"


def test_the_permission_rules_open_the_bundle_and_nothing_else() -> None:
    """The CLI matches rules against POSIX-form paths, ``//`` anchoring at the root. The path
    keeps its own flavour, so both spellings are checked on whatever host runs this."""
    assert studio_skill.permission_rules(PureWindowsPath(r"D:\Studio\voxarium_studio\skill_bundle")) == [
        "Skill(voxarium_studio:studio)",
        "Read(//d/Studio/voxarium_studio/skill_bundle/**)",
    ]
    assert studio_skill.permission_rules(PurePosixPath("/opt/studio/skill_bundle")) == [
        "Skill(voxarium_studio:studio)",
        "Read(//opt/studio/skill_bundle/**)",
    ]
    # A space is inside the parentheses, where the rule list is not split.
    assert studio_skill.rule_path(PureWindowsPath(r"C:\Users\Ann Lee\sb")) == "//c/Users/Ann Lee/sb"


@pytest.mark.parametrize(
    "path",
    [
        PureWindowsPath(r"C:\Program Files (x86)\Studio\skill_bundle"),
        PureWindowsPath(r"\\server\share\skill_bundle"),
        PurePosixPath("/opt/studio,old/skill_bundle"),
        PurePosixPath("/opt/[studio]/skill_bundle"),
    ],
)
def test_a_folder_no_rule_can_spell_gets_only_the_skill_rule(path) -> None:
    """A parenthesis or a comma would be read as the rule list's own syntax, a bracket as a
    glob, and a UNC share has no documented spelling. The Read rule is left out rather than
    written wrong: a member with ``Read`` still opens the files, one without loses only them."""
    assert studio_skill.permission_rules(path) == ["Skill(voxarium_studio:studio)"]


def test_the_installed_bundle_gets_both_rules() -> None:
    rules = studio_skill.allowed_tools()
    assert rules[0] == "Skill(voxarium_studio:studio)"
    expected = studio_skill.rule_path(studio_skill.BUNDLE)
    if expected is None:
        pytest.skip(f"this checkout's path cannot be written as a rule: {studio_skill.BUNDLE}")
    assert rules[1] == f"Read({expected}/**)"
    assert Path(studio_skill.BUNDLE).is_dir()

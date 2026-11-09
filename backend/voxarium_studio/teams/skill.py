"""The Studio's own skill: how an agent the Studio runs learns to work inside it (§10.3).

What a member can do here — the task board, delegation, the shared documents and the Markdown
they are written in, the code graph, the Studio's own files in the project — used to be spelled
out in the system prompt, a fragment per group of tools. It is a Claude Code skill now,
``voxarium_studio:studio``. Its ``SKILL.md`` is included at the start of each new task/chat
session (``required_context.py``); reference files are still read on demand. It carries the
long tail — the document dialect, the pipeline format — as files beside the instructions, and it sits
beside the tools it describes, so the two change in one commit. ``test_studio_skill.py`` holds
them together: every tool ``teams/tools.py`` exposes has to be named in it.

The skill ships inside this package, not in the managed project, and that is the decision.
The Skills catalog lists this same folder as the read-only built-in ``voxarium_studio:studio``
(§10.10), and every session receives its description and absolute instruction path as well
as the Claude Code plugin, so other backends can read the canonical instructions directly:

- **Its tools exist only in sessions the Studio runs.** The ``voxarium_studio`` MCP server is built
  in-process from the running orchestrator (§10.7), so a copy in the project's
  ``.claude/skills/`` would teach the developer's terminal tools that terminal does not have.
- **It describes this build.** A copy written into a project goes stale the moment the Studio
  updates, and the project travels to machines running other versions.
- **The Studio is a guest in ``.claude/``.** It writes ``.claude/agents/*.md`` only because
  ``--agent`` can find a member nowhere else; a skill has another way in.

That way in is a *plugin* directory (``--plugin-dir``, the SDK's ``plugins`` option): Claude
Code loads a skill from outside the project only as part of a plugin, which is why
``skill_bundle/`` carries a ``.claude-plugin/plugin.json`` and why the skill's name has the
plugin's in front of it. It is a Claude Code plugin, not a Studio plugin (§9) — nothing here
touches the plugin registry.

Two permission rules travel with it, both measured on CLI 2.1.259 — a haiku session in
``acceptEdits`` mode, one turn per variant — rather than assumed:

- ``Skill(voxarium_studio:studio)``. The invocation was allowed without it there, but every member's
  prompt sends it to this skill, so whether the skill opens must not hang on a member's
  permission mode.
- ``Read(//<bundle>/**)``. A member whose tools do not include ``Read`` was refused the
  skill's reference files ("Claude requested permissions to read from …"): they are outside
  the session's working directory. Scoped to the bundle, the rule grants nothing else.
"""

from __future__ import annotations

import logging
import re
from functools import cache
from pathlib import Path, PurePath

log = logging.getLogger(__name__)

#: The plugin's name in ``skill_bundle/.claude-plugin/plugin.json`` — the skill's namespace.
PLUGIN_NAME = "voxarium_studio"

#: The skill's folder under ``skill_bundle/skills/``, and the ``name`` in its frontmatter.
SKILL_DIRECTORY = "studio"

#: What a session invokes and what the prompts name.
SKILL_NAME = f"{PLUGIN_NAME}:{SKILL_DIRECTORY}"

#: Producer Mode has its own packaged instructions (§10.11).
PRODUCER_SKILL_DIRECTORY = "producer"
PRODUCER_SKILL_NAME = f"{PLUGIN_NAME}:{PRODUCER_SKILL_DIRECTORY}"

#: The plugin directory a session is handed.
BUNDLE = Path(__file__).resolve().parent / "skill_bundle"

# The CLI splits its rule list on commas and spaces outside parentheses and matches what is
# inside them as a gitignore pattern, so a path holding a parenthesis, a comma, a glob or an
# escape character cannot be written as a rule it will read back as that path.
# `C:\Program Files (x86)\…` is the realistic one. A space is fine: it is inside the parentheses.
_UNSAFE_IN_RULE = re.compile(r"[(),*?\[\]\\]")


def rule_path(path: PurePath) -> str | None:
    """``path`` as a Read/Edit permission rule writes an absolute path, or ``None``.

    The CLI matches those rules against POSIX-form paths — ``D:\\Studio`` is ``/d/Studio`` —
    and a leading ``//`` anchors a pattern at the filesystem root rather than at the settings
    file. The path keeps its own flavour, so a Windows path is spelled correctly on any host.
    ``None`` for what has no such spelling: a UNC share, or a character the grammar would read.
    """
    posix = path.as_posix()
    if path.drive:
        if len(path.drive) != 2 or path.drive[1] != ":":
            return None
        posix = f"/{path.drive[0].lower()}{posix[2:]}"
    if not posix.startswith("/") or _UNSAFE_IN_RULE.search(posix):
        return None
    return f"/{posix}"


def permission_rules(bundle: PurePath = BUNDLE) -> list[str]:
    """What a session is allowed so it can open the skill and every file beside it."""
    rules = [f"Skill({SKILL_NAME})"]
    where = rule_path(bundle)
    if where is not None:
        rules.append(f"Read({where}/**)")
    return rules


@cache
def allowed_tools() -> tuple[str, ...]:
    """:func:`permission_rules` for the installed bundle, worked out once per process.

    Said once rather than per run when the folder cannot be written as a rule: a member with
    ``Read`` in its tools opens the reference files anyway, and one without loses only them.
    """
    rules = permission_rules()
    if len(rules) == 1:
        log.warning(
            "The Studio skill lives at %s, which cannot be written as a permission rule; members "
            "without Read in their tools will be refused the skill's reference files",
            BUNDLE,
        )
    return tuple(rules)


def producer_allowed_tools() -> tuple[str, ...]:
    """Permit the separate Producer skill only in an active Producer chat (§10.11)."""
    return (f"Skill({PRODUCER_SKILL_NAME})",)

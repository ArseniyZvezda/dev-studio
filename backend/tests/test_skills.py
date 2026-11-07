"""The project's skills: ``.voxarium_studio/skills/<name>/SKILL.md`` and the members given them (§10.10).

What is pinned here is the folder and the roster — what counts as a skill and what it is
called, the description a session is sent, the page's writes, and a member's list surviving
the team's YAML. What a run and a chat are *told* is covered beside their other prompts, in
``test_teams.py`` and ``test_chats.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml
from fastapi.testclient import TestClient

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.main import create_app
from voxarium_studio.teams.project_skills import (
    MAX_DESCRIPTION_CHARS,
    Skill,
    SkillChangedOnDisk,
    SkillStore,
    check_name,
    check_new_name,
    describe,
    permission_rules,
    prompt_list,
)
from voxarium_studio.teams.store import TeamStore

TEAM = "team_default"
DEV = f"{TEAM}__dev"
DESIGNER = f"{TEAM}__designer"


def _skill(
    project: Path, name: str, text: str | None = None, files: dict[str, str] | None = None
) -> Path:
    """A folder under ``.voxarium_studio/skills/``; *text* is its SKILL.md, left out when ``None``.

    A SKILL.md is dated an hour back, so a write made straight after it has a later timestamp
    on any clock — a conditional write is checked against the modification time, and two writes
    inside one tick of a coarse filesystem clock would otherwise look like the same version.
    """
    folder = project / ".voxarium_studio" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    if text is not None:
        skill_file = folder / "SKILL.md"
        skill_file.write_text(text, encoding="utf-8", newline="")
        an_hour_ago = time.time() - 3600
        os.utime(skill_file, (an_hour_ago, an_hour_ago))
    for relative, content in (files or {}).items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return folder


def _store(project: Path) -> SkillStore:
    return SkillStore(ProjectPaths.for_project(project))


def _entry(name: str, directory: PurePosixPath | PureWindowsPath, description: str = "") -> Skill:
    return Skill(
        name=name,
        directory=directory,  # type: ignore[arg-type]
        description=description,
        warning=None,
        files=(),
        file_count=0,
        updated_at="",
    )


# ------------------------------------------------------------------ what a skill is


def test_a_skill_is_a_folder_with_a_skill_md_and_is_named_after_the_folder(project: Path) -> None:
    """The folder's name is the skill's everywhere — the frontmatter's ``name`` is not consulted,
    so a skill copied in under another name is called what it was put as."""
    _skill(
        project,
        "level-prototyping",
        "---\nname: blockout\ndescription: >-\n  Blocks out a level\n  from a brief.\n---\n\nSteps.\n",
        {"reference.md": "r", "scripts/lint.py": "x", "node_modules/dep/index.js": "n"},
    )
    _skill(project, "no-instructions-yet", files={"notes.txt": "n"})
    _skill(project, ".hidden", "---\ndescription: hidden\n---\n")
    (project / ".voxarium_studio" / "skills" / "README.md").write_text("a file, not a skill", encoding="utf-8")

    skills, invalid = _store(project).scan()

    assert [skill.name for skill in skills] == ["level-prototyping"]
    skill = skills[0]
    assert skill.description == "Blocks out a level from a brief.", "folded onto one line"
    assert skill.warning is None
    assert skill.files == ("reference.md", "scripts/lint.py"), "not SKILL.md, not dependencies"
    assert skill.file_count == 2
    assert [entry.name for entry in invalid] == ["no-instructions-yet"]
    assert "SKILL.md" in invalid[0].error


@pytest.mark.parametrize(
    ("text", "warned"),
    [
        ("Just instructions, no frontmatter.\n", "No frontmatter"),
        ("---\ndescription: [unclosed\n---\n", "does not parse"),
        ("---\n- a list\n---\n", "not a set of key: value"),
        ("---\nname: x\n---\nbody\n", "No description"),
        ("---\ndescription:   \n---\n", "No description"),
    ],
)
def test_a_skill_that_says_nothing_about_when_to_use_it_is_still_a_skill(
    text: str, warned: str
) -> None:
    """The instructions are still there to be read — only the line saying when is missing, and
    that is what the page warns about."""
    description, warning = describe(text)
    assert description == ""
    assert warning is not None and warned in warning


def test_a_description_is_read_from_a_crlf_file_and_cut_at_the_format_limit() -> None:
    assert describe("---\r\nname: x\r\ndescription: Windows line endings\r\n---\r\nbody\r\n") == (
        "Windows line endings",
        None,
    )
    description, warning = describe(f"---\ndescription: {'word ' * 400}\n---\n")
    assert len(description) == MAX_DESCRIPTION_CHARS and description.endswith("…")
    assert warning is not None and str(MAX_DESCRIPTION_CHARS) in warning


@pytest.mark.parametrize(
    "name",
    ["", "a/b", "a\\b", "..", ".hidden", " padded", "trailing.", "what?", "con", "a:b", "x" * 129],
)
def test_a_name_a_folder_could_not_carry_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        check_name(name)


def test_a_folder_copied_in_is_read_whatever_it_is_called_but_new_ones_are_kebab_case() -> None:
    assert check_name("Game Designer (old)") == "Game Designer (old)"
    assert check_new_name("level-prototyping") == "level-prototyping"
    for name in ("Game Designer", "game_designer", "-leading", "trailing-", "UPPER", "x" * 65):
        with pytest.raises(ValueError):
            check_new_name(name)


# ------------------------------------------------------------------ writing from the page


def test_a_created_skill_reads_back_with_its_description(project: Path) -> None:
    store = _store(project)
    created = store.create("level-prototyping", "Blocks out a level: quickly.\nFrom a brief.")
    assert created.description == "Blocks out a level: quickly. From a brief."
    front = yaml.safe_load(created.skill_file.read_text(encoding="utf-8").split("---\n")[1])
    assert front == {
        "name": "level-prototyping",
        "description": "Blocks out a level: quickly. From a brief.",
    }

    with pytest.raises(FileExistsError):
        store.create("level-prototyping")
    with pytest.raises(ValueError):
        store.create("Level Prototyping")


def test_a_write_keeps_the_files_line_endings_and_refuses_a_stale_expectation(
    project: Path,
) -> None:
    folder = _skill(project, "crlf", "---\r\ndescription: old\r\n---\r\nBody\r\n")
    store = _store(project)

    skill, text, crlf = store.read_text("crlf")
    assert crlf and "\r" not in text, "the editor gets \\n, and the flag"

    written = store.write(
        "crlf", "---\ndescription: new\n---\nBody\nMore\n", expected_updated_at=skill.updated_at
    )
    assert written.description == "new"
    assert (folder / "SKILL.md").read_bytes() == b"---\r\ndescription: new\r\n---\r\nBody\r\nMore\r\n"

    with pytest.raises(SkillChangedOnDisk):
        store.write("crlf", "late", expected_updated_at=skill.updated_at)
    with pytest.raises(FileNotFoundError):
        store.write("nowhere", "text")


def test_a_folder_that_is_not_a_skill_yet_can_be_given_its_skill_md(project: Path) -> None:
    _skill(project, "draft")
    store = _store(project)
    assert store.get("draft") is None
    store.write("draft", "---\ndescription: Now a skill.\n---\n")
    skill = store.get("draft")
    assert skill is not None and skill.description == "Now a skill."


async def test_a_deleted_skill_takes_its_folder_with_it(project: Path) -> None:
    folder = _skill(project, "gone", "---\ndescription: d\n---\n", {"a/b.txt": "x"})
    store = _store(project)
    await store.delete("gone")
    assert not folder.exists()
    assert not list(folder.parent.iterdir()), "nothing left aside"
    with pytest.raises(FileNotFoundError):
        await store.delete("gone")


# ------------------------------------------------------------------ what a member is told


def test_a_members_names_resolve_exactly_and_once_each(project: Path) -> None:
    """Exactly, not as the filesystem would answer — on Windows ``Tester`` would open ``tester``,
    and a roster that works on one checkout and not on another is worse than a picked name."""
    _skill(project, "tester", "---\ndescription: Tests.\n---\n")
    _skill(project, "draft")
    resolved = _store(project).for_member(["tester", "Tester", "tester", "draft", "bad/name"])
    assert [skill.name for skill in resolved.found] == ["tester"]
    assert resolved.missing == ("Tester", "draft", "bad/name")


def test_each_skill_is_listed_with_where_to_read_it_and_gets_a_read_rule_of_its_own() -> None:
    tester = _entry("tester", PureWindowsPath(r"D:\Game\.voxarium_studio\skills\tester"), "Writes test plans.")
    blank = _entry("blank", PurePosixPath("/srv/game/.voxarium_studio/skills/blank"))
    odd = _entry("odd", PureWindowsPath(r"C:\Program Files (x86)\Game\.voxarium_studio\skills\odd"))

    assert prompt_list([tester, blank]).splitlines() == [
        r"- tester: Writes test plans. (instructions: D:\Game\.voxarium_studio\skills\tester\SKILL.md)",
        "- blank (instructions: /srv/game/.voxarium_studio/skills/blank/SKILL.md)",
    ]
    # A parenthesis is the rule list's own syntax, so that folder gets no rule rather than a
    # wrong one: a member with Read still opens it, one without loses only it.
    assert permission_rules([tester, blank, odd]) == [
        "Read(//d/Game/.voxarium_studio/skills/tester/**)",
        "Read(//srv/game/.voxarium_studio/skills/blank/**)",
    ]


# ------------------------------------------------------------------ the API


def _yaml_member(project: Path, short_id: str) -> dict:
    team_file = project / ".voxarium_studio" / "teams" / "default-team.yaml"
    roster = yaml.safe_load(team_file.read_text(encoding="utf-8"))
    return next(member for member in roster["members"] if member["id"] == short_id)


def test_the_page_creates_a_skill_and_lists_who_has_it(client: TestClient) -> None:
    created = client.post(
        "/api/skills", json={"name": "level-prototyping", "description": "Blocks out levels."}
    )
    assert created.status_code == 201, created.text
    page = created.json()
    assert page["path"] == ".voxarium_studio/skills"
    local = [skill for skill in page["skills"] if skill["source"] is None]
    assert [skill["name"] for skill in local] == ["level-prototyping"]
    assert local[0]["description"] == "Blocks out levels."
    assert local[0]["used_by"] == [], "given to nobody by being created"

    assert client.post("/api/skills", json={"name": "level-prototyping"}).status_code == 409
    assert client.post("/api/skills", json={"name": "Level Prototyping"}).status_code == 400

    given = client.patch(f"/api/teams/{TEAM}/members/{DEV}", json={"skills": ["level-prototyping"]})
    assert given.status_code == 200, given.text
    listed = client.get("/api/skills/level-prototyping").json()
    assert [(user["id"], user["name"]) for user in listed["used_by"]] == [(DEV, "Igor")]


def test_a_members_skills_travel_through_the_team_yaml(client: TestClient, project: Path) -> None:
    _skill(project, "tester", "---\ndescription: Tests.\n---\n")
    _skill(project, "writer", "---\ndescription: Writes.\n---\n")
    member = f"/api/teams/{TEAM}/members/{DEV}"

    response = client.patch(member, json={"skills": ["writer", "tester", "writer"]})
    assert response.status_code == 200, response.text
    assert response.json()["skills"] == ["writer", "tester"], "each once, in the order given"
    assert _yaml_member(project, "dev")["skills"] == ["writer", "tester"]
    assert "skills" not in _yaml_member(project, "designer"), "nobody else was given anything"

    cleared = client.patch(member, json={"skills": None})
    assert cleared.status_code == 200 and cleared.json()["skills"] == []
    assert "skills" not in _yaml_member(project, "dev"), "an empty list is not written out"


def test_only_a_skill_being_added_has_to_exist(client: TestClient, project: Path) -> None:
    """A skill deleted on disk stays on its members until somebody takes it off — and taking off a
    *different* one sends the whole list, which must not be refused over the missing name."""
    tester = _skill(project, "tester", "---\ndescription: Tests.\n---\n")
    _skill(project, "writer", "---\ndescription: Writes.\n---\n")
    member = f"/api/teams/{TEAM}/members/{DEV}"
    assert client.patch(member, json={"skills": ["tester", "writer"]}).status_code == 200

    unknown = client.patch(member, json={"skills": ["tester", "writer", "nowhere"]})
    assert unknown.status_code == 400 and "nowhere" in unknown.json()["detail"]
    assert client.patch(member, json={"skills": ["../escape"]}).status_code == 400

    (tester / "SKILL.md").unlink()
    tester.rmdir()
    kept = client.patch(member, json={"skills": ["tester"]})
    assert kept.status_code == 200, kept.text

    page = client.get("/api/skills").json()
    assert [entry["name"] for entry in page["missing"]] == ["tester"]
    assert [user["id"] for user in page["missing"][0]["used_by"]] == [DEV]


def test_skill_md_is_edited_over_the_api(client: TestClient, project: Path) -> None:
    _skill(project, "tester", "---\ndescription: Tests.\n---\nBody\n")
    detail = client.get("/api/skills/tester").json()
    assert detail["content"] == "---\ndescription: Tests.\n---\nBody\n"
    assert detail["crlf"] is False

    saved = client.put(
        "/api/skills/tester",
        json={
            "content": "---\ndescription: Plans tests.\n---\nBody\n",
            "expected_updated_at": detail["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["description"] == "Plans tests."
    assert saved.json()["updated_at"] != detail["updated_at"]

    stale = client.put(
        "/api/skills/tester", json={"content": "x", "expected_updated_at": detail["updated_at"]}
    )
    assert stale.status_code == 409
    assert client.get("/api/skills/nowhere").status_code == 404


def test_deleting_a_skill_takes_it_off_every_member(client: TestClient, project: Path) -> None:
    folder = _skill(project, "tester", "---\ndescription: Tests.\n---\n")
    _skill(project, "writer", "---\ndescription: Writes.\n---\n")
    client.patch(f"/api/teams/{TEAM}/members/{DEV}", json={"skills": ["tester", "writer"]})
    client.patch(f"/api/teams/{TEAM}/members/{DESIGNER}", json={"skills": ["tester"]})

    page = client.delete("/api/skills/tester")
    assert page.status_code == 200, page.text
    assert not folder.exists()
    assert [skill["name"] for skill in page.json()["skills"] if skill["source"] is None] == ["writer"]
    assert page.json()["missing"] == []
    assert _yaml_member(project, "dev")["skills"] == ["writer"]
    assert "skills" not in _yaml_member(project, "designer")

    assert client.delete("/api/skills/tester").status_code == 404


def test_a_skill_gone_from_disk_comes_off_its_members_by_the_same_delete(
    client: TestClient, project: Path
) -> None:
    folder = _skill(project, "tester", "---\ndescription: Tests.\n---\n")
    client.patch(f"/api/teams/{TEAM}/members/{DEV}", json={"skills": ["tester"]})
    (folder / "SKILL.md").unlink()
    folder.rmdir()

    page = client.delete("/api/skills/tester")
    assert page.status_code == 200, page.text
    assert page.json()["missing"] == []
    assert "skills" not in _yaml_member(project, "dev")


def test_a_skill_the_yaml_names_loads_whatever_the_folder_holds(project: Path) -> None:
    """Hand-edited YAML is the roster too: a name there loads as written, even one this checkout
    has no folder for — a sync is not where a skill is taken off anybody."""
    team_file = project / ".voxarium_studio" / "teams" / "default-team.yaml"
    team_file.write_text(
        team_file.read_text(encoding="utf-8").replace(
            "    allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]",
            "    allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]\n    skills: [tester, not-here]",
        ),
        encoding="utf-8",
    )
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        team = client.get(f"/api/teams/{TEAM}").json()
    dev = next(member for member in team["members"] if member["id"] == DEV)
    assert dev["skills"] == ["tester", "not-here"]


def test_an_empty_or_single_skills_key_does_not_cost_the_team_file(project: Path) -> None:
    """`skills:` left empty is YAML null, and a lone name is a string. Refusing either would skip
    the whole team file — every member, not just this key."""
    team_file = project / ".voxarium_studio" / "teams" / "default-team.yaml"
    team_file.write_text(
        team_file.read_text(encoding="utf-8")
        .replace(
            "    allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]",
            "    allowed_tools: [Read, Write, Edit, Bash, Grep, Glob]\n    skills:",
        )
        .replace(
            "    allowed_tools: [Read, Write, Grep, Glob]",
            "    allowed_tools: [Read, Write, Grep, Glob]\n    skills: tester",
        ),
        encoding="utf-8",
    )
    definitions = TeamStore(ProjectPaths.for_project(project)).load_definitions()
    assert len(definitions) == 1, "the team file still loads"
    by_id = {member.id: member for member in definitions[0].members}
    assert by_id["dev"].skills == []
    assert by_id["designer"].skills == ["tester"]


def test_a_project_initialised_before_skills_gains_the_folder_on_start(project: Path) -> None:
    skills = project / ".voxarium_studio" / "skills"
    skills.rmdir()
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        assert all(skill["source"] for skill in client.get("/api/skills").json()["skills"])
    assert skills.is_dir()

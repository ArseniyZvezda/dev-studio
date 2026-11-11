"""Importing what the agent runtimes remember on their own into project memory (§10.12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.teams import agent_memory
from voxarium_studio.teams.agent_memory import import_candidates, scan
from voxarium_studio.teams.memory import MAX_CONTENT_CHARS, MAX_DESCRIPTION_CHARS, MemoryStore


@pytest.fixture(autouse=True)
def no_qwen_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("QWEN_RUNTIME_DIR", "QWEN_HOME"):
        monkeypatch.delenv(name, raising=False)


REAL_SLUG = agent_memory._slug


@pytest.fixture(autouse=True)
def short_project_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """A temp path as a folder name, under a temp path, is past what Windows will create."""
    monkeypatch.setattr(agent_memory, "_slug", lambda path: _slug(path).casefold())


def _slug(root: Path) -> str:
    return f"D--{root.name.replace(' ', '-')}"


def test_a_project_folder_is_the_path_with_dashes() -> None:
    assert REAL_SLUG(Path("D:/Perforce/Arseniy.Zvezda/My Game")) in (
        "d--perforce-arseniy-zvezda-my-game", "d-perforce-arseniy-zvezda-my-game")


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _claude(home: Path, root: Path, name: str, text: str) -> Path:
    return _write(home / ".claude" / "projects" / _slug(root) / "memory" / name, text)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = tmp_path / "Game Project"
    project.mkdir()
    return project


@pytest.fixture
def store(root: Path) -> MemoryStore:
    return MemoryStore(ProjectPaths.for_project(root), bus=AsyncMock())


def _by_backend(root: Path, store: MemoryStore) -> dict[str, agent_memory.AgentMemory]:
    return {source.backend: source for source in scan(root, store)}


def test_every_backend_is_listed_even_with_nothing_to_offer(root: Path, store: MemoryStore) -> None:
    sources = scan(root, store)
    assert [source.backend for source in sources] == ["claude_code", "codex", "qwen"]
    assert all(source.candidates == [] and source.locations for source in sources)
    assert sources[0].locations == ["~/.claude/projects/<project>/memory"]


def test_claude_topic_files_become_candidates(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    _claude(agent_skills_home, root, "MEMORY.md", "- [Build](build.md) — hook\n")
    _claude(
        agent_skills_home, root, "build.md",
        "---\nname: build-uses-ue-5-4\ndescription: Builds with\n  Unreal 5.4\nmetadata:\n  type: project\n---\n\n"
        "Plugins pin 5.4.\r\n",
    )
    _claude(agent_skills_home, root, "Plain Note.md", "# Heading\n\nNo frontmatter here.\n")
    _claude(agent_skills_home, root, "old.md", "---\ntype: feedback\n---\nUse tabs.\n")
    # Another project's memory is not this one's.
    _write(agent_skills_home / ".claude" / "projects" / "D--other" / "memory" / "x.md", "nope")

    items = {item.name: item for item in _by_backend(root, store)["claude_code"].candidates}
    assert set(items) == {"build-uses-ue-5-4", "plain-note", "old"}
    build = items["build-uses-ue-5-4"]
    assert (build.description, build.content, build.kind) == ("Builds with Unreal 5.4", "Plugins pin 5.4.", "project")
    assert (build.scope, build.state, build.related) == ("project", "new", True)
    assert build.source.startswith("~/.claude/projects/") and build.source.endswith("/memory/build.md")
    assert items["plain-note"].description == "Heading"
    assert (items["old"].kind, items["old"].description) == ("feedback", "Use tabs.")


def test_the_project_folder_is_matched_without_case_and_by_its_repository(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    (root / ".git").mkdir()
    nested = root / "game"
    nested.mkdir()
    folder = agent_skills_home / ".claude" / "projects" / _slug(root).lower() / "memory"
    _write(folder / "fact.md", "---\ndescription: d\n---\nbody\n")
    nested_store = MemoryStore(ProjectPaths.for_project(nested), bus=AsyncMock())
    assert [item.name for item in _by_backend(nested, nested_store)["claude_code"].candidates] == ["fact"]


def test_codex_handbook_sections_are_global_candidates(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    _write(
        agent_skills_home / ".codex" / "memories" / "MEMORY.md",
        "preamble\n\n# Task Group: Voxel export\nscope: exporter\n"
        f"applies_to: cwd={str(root)}\n\n```\n# not a heading\n```\n\n"
        "# Task Group: Somewhere else\napplies_to: cwd=/elsewhere\n\n# Empty\n",
    )
    codex = _by_backend(root, store)["codex"].candidates
    assert [(item.name, item.related, item.scope) for item in codex] == [
        ("task-group-voxel-export", True, "global"),
        ("task-group-somewhere-else", False, "global"),
    ]
    assert codex[0].description == "Task Group: Voxel export"
    assert "# not a heading" in codex[0].content
    assert codex[0].id == "codex:MEMORY.md#task-group-voxel-export"


def test_qwen_reads_its_folder_and_what_save_memory_appended(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    _write(
        agent_skills_home / ".qwen" / "projects" / _slug(root).lower() / "memory" / "style.md",
        "---\nname: Art style\ndescription: Low-poly\ntype: project\n---\nFlat shaded.\n",
    )
    _write(root / ".qwen" / "memory" / "local.md", "Kept in the project.\n")
    _write(
        root / "QWEN.md",
        "# Project\n\n## Qwen Added Memories\n- Tests run with pytest -q\n- " + "long " * 80 + "\n\n## Other\n- not a memory\n",
    )
    _write(agent_skills_home / ".qwen" / "QWEN.md", "## Qwen Added Memories\n- I prefer tabs\n")

    qwen = _by_backend(root, store)["qwen"].candidates
    names = [item.name for item in qwen]
    assert names[:3] == ["art-style", "local", "tests-run-with-pytest-q"]
    assert len(qwen) == 5
    long = qwen[3]
    assert len(long.description) == MAX_DESCRIPTION_CHARS and long.content.startswith("long long")
    assert (qwen[2].content, qwen[2].scope, qwen[2].source) == ("", "project", "QWEN.md")
    assert (qwen[4].name, qwen[4].scope, qwen[4].related) == ("i-prefer-tabs", "global", False)


def test_names_are_settled_across_backends_and_long_text_is_cut(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    _claude(agent_skills_home, root, "build.md", "---\ndescription: from claude\n---\n" + "x" * (MAX_CONTENT_CHARS + 50))
    _claude(agent_skills_home, root, "device.md", "---\nname: aux\n---\nreserved on Windows\n")
    _claude(agent_skills_home, root, "Память.md", "Кириллица в имени\n")
    _write(root / ".qwen" / "memory" / "build.md", "---\ndescription: from qwen\n---\nbody\n")

    sources = _by_backend(root, store)
    claude = {item.description: item for item in sources["claude_code"].candidates}
    assert claude["from claude"].name == "build"
    assert claude["from claude"].truncated and len(claude["from claude"].content) <= MAX_CONTENT_CHARS
    assert "cut on import" in claude["from claude"].content
    assert claude["reserved on Windows"].name == "aux-memory"
    assert claude["Кириллица в имени"].name == "claude-code-memory"
    assert sources["qwen"].candidates[0].name == "build-2"


async def test_import_writes_chosen_entries_and_never_overwrites_unasked(
    root: Path, store: MemoryStore, agent_skills_home: Path
) -> None:
    source = _claude(agent_skills_home, root, "build.md", "---\ndescription: Unreal 5.4\n---\nPins.\n")
    _claude(agent_skills_home, root, "style.md", "---\ndescription: Low-poly\n---\n")
    await store.write("style", description="Mine", content="keep")

    listed = {item.name: item for item in _by_backend(root, store)["claude_code"].candidates}
    assert (listed["build"].state, listed["style"].state) == ("new", "differs")

    outcome = await import_candidates(
        root, store, {listed["build"].id: False, listed["style"].id: False, "claude_code:gone/x.md": False}
    )
    assert outcome.imported == ["build"]
    assert [(item["name"], item["reason"]) for item in outcome.skipped] == [("style", "exists"), ("", "gone")]
    entry = store.get("build")
    assert entry is not None and (entry.description, entry.content, entry.author) == ("Unreal 5.4", "Pins.", None)
    assert store.get("style").content == "keep"
    # The runtime's own file is read, never written.
    assert source.read_text(encoding="utf-8") == "---\ndescription: Unreal 5.4\n---\nPins.\n"

    again = {item.name: item for item in _by_backend(root, store)["claude_code"].candidates}
    assert again["build"].state == "same"
    replaced = await import_candidates(root, store, {again["build"].id: True, again["style"].id: True})
    assert replaced.imported == ["style"]
    assert replaced.skipped == [{"id": again["build"].id, "name": "build", "reason": "same"}]
    assert store.get("style").description == "Low-poly"


def test_the_page_lists_and_imports(client: TestClient, project: Path, agent_skills_home: Path) -> None:
    root = ProjectPaths.for_project(project).root
    _claude(agent_skills_home, root, "build.md", "---\ndescription: Unreal 5.4\n---\nPins.\n")

    listing = client.get("/api/agent-memory")
    assert listing.status_code == 200, listing.text
    sources = listing.json()["sources"]
    assert [source["backend"] for source in sources] == ["claude_code", "codex", "qwen"]
    candidate = sources[0]["candidates"][0]
    assert (candidate["name"], candidate["state"], candidate["content"]) == ("build", "new", "Pins.")

    done = client.post("/api/agent-memory/import", json={"items": [{"id": candidate["id"]}]})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["imported"] == ["build"] and body["skipped"] == []
    assert [entry["name"] for entry in body["memory"]["entries"]] == ["build"]
    assert client.get("/api/agent-memory").json()["sources"][0]["candidates"][0]["state"] == "same"
    # `import` stays an ordinary entry name under /api/memory.
    assert client.get("/api/memory/import").status_code == 404

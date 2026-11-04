"""`.mcp.json` — parsing, editing without losing anything, and what agents get (§10.7)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from voxarium_studio.core import mcp
from voxarium_studio.core.config import AppConfig
from voxarium_studio.core.paths import ProjectPaths


def write_mcp(root: Path, document: dict) -> Path:
    path = root / ".mcp.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def from_file(payload: dict) -> list[dict]:
    """The servers that came out of ``.mcp.json``.

    The page also lists the Studio's own built-in server (§10.7), which is in no file and
    heads the list — so every assertion about "what the file holds" has to say so.
    """
    return [entry for entry in payload["servers"] if not entry["builtin"]]


# ---------------------------------------------------------------- the file model


def test_a_missing_file_reads_as_empty_rather_than_an_error(project: Path) -> None:
    file = mcp.read_mcp_file(project / ".mcp.json")
    assert file.exists is False
    assert file.error is None
    assert file.servers == {}


def test_stdio_entries_may_omit_their_type(project: Path) -> None:
    write_mcp(project, {"mcpServers": {"fs": {"command": "npx", "args": ["-y", "server"]}}})
    file = mcp.read_mcp_file(project / ".mcp.json")
    server = file.servers["fs"]
    assert isinstance(server, mcp.StdioServer)
    assert server.type == "stdio"
    assert server.args == ["-y", "server"]


def test_a_url_without_a_type_reads_as_a_remote_server(project: Path) -> None:
    write_mcp(project, {"mcpServers": {"api": {"url": "https://example.test/mcp"}}})
    server = mcp.read_mcp_file(project / ".mcp.json").servers["api"]
    assert isinstance(server, mcp.RemoteServer)
    assert server.type == "http"


def test_one_bad_entry_does_not_hide_the_good_ones(project: Path) -> None:
    write_mcp(
        project,
        {"mcpServers": {"ok": {"command": "node"}, "broken": {"type": "carrier-pigeon"}}},
    )
    file = mcp.read_mcp_file(project / ".mcp.json")
    assert list(file.servers) == ["ok"]
    assert "carrier-pigeon" in file.invalid["broken"]
    assert file.error is None


def test_a_malformed_document_reports_where_and_refuses_writes(project: Path) -> None:
    path = project / ".mcp.json"
    path.write_text('{"mcpServers": {', encoding="utf-8")
    file = mcp.read_mcp_file(path)
    assert file.error is not None and "line 1" in file.error

    try:
        mcp.upsert_server(file, "fs", mcp.StdioServer(command="node"))
    except mcp.McpFileError as exc:
        assert "could not be parsed" in str(exc)
    else:  # pragma: no cover - the guard is the whole point of the test
        raise AssertionError("a broken document must not be rewritten")


def test_an_edit_keeps_every_key_the_studio_does_not_model(project: Path) -> None:
    """The file belongs to Claude Code, not to us — a field we do not render must survive."""
    write_mcp(
        project,
        {
            "$schema": "https://example.test/mcp.schema.json",
            "mcpServers": {
                "fs": {"command": "old", "somethingNew": {"keep": True}},
                "other": {"command": "left-alone"},
            },
        },
    )
    path = project / ".mcp.json"
    file = mcp.read_mcp_file(path)
    document = mcp.upsert_server(file, "fs", mcp.StdioServer(command="new", args=["--flag"]))
    mcp.write_document(path, document, previous=file.text)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["$schema"] == "https://example.test/mcp.schema.json"
    assert written["mcpServers"]["fs"] == {
        "somethingNew": {"keep": True},
        "type": "stdio",
        "command": "new",
        "args": ["--flag"],
    }
    assert written["mcpServers"]["other"] == {"command": "left-alone"}


def test_an_empty_env_is_not_added_to_the_file(project: Path) -> None:
    path = project / ".mcp.json"
    file = mcp.read_mcp_file(path)
    document = mcp.upsert_server(file, "fs", mcp.StdioServer(command="node"))
    mcp.write_document(path, document, previous=file.text)
    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["fs"] == {
        "type": "stdio",
        "command": "node",
    }


def test_a_crlf_file_stays_crlf(project: Path) -> None:
    path = project / ".mcp.json"
    path.write_bytes(b'{\r\n  "mcpServers": {}\r\n}\r\n')
    file = mcp.read_mcp_file(path)
    mcp.write_document(path, mcp.upsert_server(file, "fs", mcp.StdioServer(command="node")),
                       previous=file.text)
    assert b"\r\n" in path.read_bytes()


def test_raw_writes_keep_the_authors_formatting(project: Path) -> None:
    path = project / ".mcp.json"
    text = '{\n    "mcpServers": {\n        "fs": { "command": "node" }\n    }\n}\n'
    mcp.write_text(path, text)
    assert path.read_text(encoding="utf-8") == text


def test_a_name_that_cannot_appear_in_a_tool_name_is_refused() -> None:
    for bad in ("has space", "with/slash", "", "-leading"):
        try:
            mcp.check_name(bad)
        except mcp.McpFileError:
            continue
        raise AssertionError(f"{bad!r} should not be a valid server name")
    assert mcp.check_name(" github-mcp ") == "github-mcp"


# ---------------------------------------------------------------- what agents get


def test_disabled_servers_are_withheld_from_agent_sessions(project: Path) -> None:
    write_mcp(
        project,
        {"mcpServers": {"kept": {"command": "node"}, "off": {"command": "node"}}},
    )
    paths = ProjectPaths.for_project(project)
    config = AppConfig()
    config.mcp.disabled = ["off"]

    servers = mcp.enabled_servers(paths, config)
    assert list(servers) == ["kept"]
    assert mcp.allowed_tool_names(servers) == ["mcp__kept"]


def test_a_broken_file_yields_no_servers_rather_than_a_broken_session(project: Path) -> None:
    (project / ".mcp.json").write_text("not json at all", encoding="utf-8")
    assert mcp.enabled_servers(ProjectPaths.for_project(project), AppConfig()) == {}


# ---------------------------------------------------------------- the REST surface


def test_the_page_payload_describes_the_file_and_the_toggles(
    client: TestClient, project: Path
) -> None:
    write_mcp(project, {"mcpServers": {"fs": {"command": "definitely-not-on-path"}}})

    payload = client.get("/api/mcp").json()
    assert payload["exists"] is True
    assert payload["strict"] is True
    entry = from_file(payload)[0]
    assert entry["name"] == "fs"
    assert entry["enabled"] is True
    assert entry["available"] is False
    assert entry["tool_prefix"] == "mcp__fs__"


def test_creating_editing_and_deleting_a_server_over_rest(
    client: TestClient, project: Path
) -> None:
    created = client.put(
        "/api/mcp/servers/github",
        json={"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
    )
    assert created.status_code == 200
    assert [s["name"] for s in from_file(created.json())] == ["github"]

    edited = client.put(
        "/api/mcp/servers/github", json={"type": "http", "url": "https://example.test/mcp"}
    ).json()
    assert from_file(edited)[0]["url"] == "https://example.test/mcp"
    assert from_file(edited)[0]["available"] is None

    removed = client.delete("/api/mcp/servers/github").json()
    assert from_file(removed) == []
    assert client.delete("/api/mcp/servers/github").status_code == 404


def test_a_server_with_neither_command_nor_url_is_refused(client: TestClient) -> None:
    response = client.put("/api/mcp/servers/empty", json={})
    assert response.status_code == 400
    assert "command" in response.json()["detail"]


def test_enabling_and_disabling_persists_to_config_yaml(
    client: TestClient, project: Path
) -> None:
    write_mcp(project, {"mcpServers": {"fs": {"command": "node"}}})

    disabled = client.post("/api/mcp/servers/fs/disable").json()
    assert from_file(disabled)[0]["enabled"] is False
    assert "disabled:" in (project / "voxarium_studio" / "config.yaml").read_text(encoding="utf-8")

    enabled = client.post("/api/mcp/servers/fs/enable").json()
    assert from_file(enabled)[0]["enabled"] is True
    assert client.post("/api/mcp/servers/nope/disable").status_code == 404


def test_deleting_a_disabled_server_forgets_the_toggle(client: TestClient, project: Path) -> None:
    """Otherwise a later server that reused the name would arrive switched off."""
    write_mcp(project, {"mcpServers": {"fs": {"command": "node"}}})
    client.post("/api/mcp/servers/fs/disable")
    client.delete("/api/mcp/servers/fs")

    client.put("/api/mcp/servers/fs", json={"command": "node"})
    assert from_file(client.get("/api/mcp").json())[0]["enabled"] is True


def test_the_raw_editor_reports_bad_json_instead_of_writing_it(
    client: TestClient, project: Path
) -> None:
    write_mcp(project, {"mcpServers": {"fs": {"command": "node"}}})
    before = (project / ".mcp.json").read_text(encoding="utf-8")

    response = client.put("/api/mcp/file", json={"content": "{oops"})
    assert response.status_code == 400
    assert (project / ".mcp.json").read_text(encoding="utf-8") == before

    ok = client.put("/api/mcp/file", json={"content": '{"mcpServers": {"a": {"url": "x"}}}'})
    assert [s["name"] for s in from_file(ok.json())] == ["a"]


def test_strict_mode_round_trips_through_settings(client: TestClient) -> None:
    assert client.patch("/api/mcp/settings", json={"strict": False}).json()["strict"] is False
    assert client.get("/api/mcp").json()["strict"] is False
    assert client.get("/api/settings").json()["mcp"]["strict"] is False


# ---------------------------------------------------------------- the built-in server


def builtin(payload: dict) -> dict:
    return next(entry for entry in payload["servers"] if entry["builtin"])


def test_the_studios_own_server_is_listed_even_with_no_file(client: TestClient) -> None:
    """A page called "MCP Servers" that omits the one every agent uses is lying (§10.7)."""
    payload = client.get("/api/mcp").json()
    assert payload["exists"] is False, "no .mcp.json in a fresh project"

    entry = builtin(payload)
    assert entry["name"] == "voxarium_studio"
    assert entry["type"] == "builtin"
    assert entry["enabled"] is True
    assert entry["available"] is True
    assert entry["tool_prefix"] == "mcp__voxarium_studio__"
    assert "create_task" in entry["tools"] and "list_tasks" in entry["tools"]
    assert payload["servers"][0] is entry or payload["servers"][0]["name"] == "voxarium_studio"


def test_the_built_in_server_cannot_be_deleted_or_replaced(client: TestClient) -> None:
    removed = client.delete("/api/mcp/servers/voxarium_studio")
    assert removed.status_code == 400
    assert "built-in" in removed.json()["detail"]

    replaced = client.put("/api/mcp/servers/voxarium_studio", json={"command": "node"})
    assert replaced.status_code == 400

    # And neither attempt wrote anything: the file is still absent.
    assert client.get("/api/mcp").json()["exists"] is False


def test_the_built_in_server_can_be_switched_off_like_any_other(
    client: TestClient, project: Path
) -> None:
    """Disabling is the coherent half of "built-in": no file entry to delete, but
    `mcp.disabled` is the Studio's own list and works on it unchanged."""
    off = client.post("/api/mcp/servers/voxarium_studio/disable")
    assert off.status_code == 200
    assert builtin(off.json())["enabled"] is False
    config = (project / "voxarium_studio" / "config.yaml").read_text(encoding="utf-8")
    assert "voxarium_studio" in config.split("mcp:")[1].split("vcs:")[0]

    on = client.post("/api/mcp/servers/voxarium_studio/enable").json()
    assert builtin(on)["enabled"] is True


async def test_disabling_it_takes_the_studio_tools_off_the_run(ctx) -> None:
    """`build_toolset` reads the same list, so the switch reaches the session."""
    from sqlmodel import select

    from voxarium_studio.models.teams import TeamMember
    from voxarium_studio.teams.tools import build_toolset

    async with ctx.db.session() as session:
        member = (await session.exec(select(TeamMember))).first()
    task = await ctx.teams.create_task(
        team_id=member.team_id, assignee_id=member.id, title="t", brief="b"
    )

    if build_toolset(ctx.teams, member=member, task=task) is None:
        import pytest

        pytest.skip("claude-agent-sdk is not installed")

    ctx.config.mcp.disabled = ["voxarium_studio"]
    assert build_toolset(ctx.teams, member=member, task=task) is None


def test_a_file_entry_named_voxarium_studio_is_reported_rather_than_listed_twice(
    client: TestClient, project: Path
) -> None:
    """The orchestrator merges the Studio's tools over the project's, so it never runs."""
    write_mcp(project, {"mcpServers": {"voxarium_studio": {"command": "node"}}})

    payload = client.get("/api/mcp").json()
    assert [entry["name"] for entry in payload["servers"]] == ["voxarium_studio"]
    assert builtin(payload)["builtin"] is True
    assert any("shadowed" in entry["error"] for entry in payload["invalid"])

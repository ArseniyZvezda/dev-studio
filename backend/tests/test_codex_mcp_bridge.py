"""Real MCP stdio round trips into the Studio, without any model/API calls (§10.6)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from sqlmodel import select

from voxarium_studio.models.teams import TeamMember, TeamTask
from voxarium_studio.teams.backends.mcp_bridge import TOKEN_ENV, bridge_servers
from voxarium_studio.teams.tools import build_toolset

pytest.importorskip("claude_agent_sdk")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TEAM = "team_default"
DEV = f"{TEAM}__dev"


@asynccontextmanager
async def connect(config):
    async with stdio_client(StdioServerParameters(**config)) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            yield client


async def test_project_authoring_round_trip(ctx):
    """Structured schemas ($defs, aliases and trigger unions) survive the real MCP bridge."""
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
    toolset = build_toolset(ctx.teams, member=dev)
    async with bridge_servers(toolset.mcp_servers, 1_048_576) as servers:
        async with connect(servers["voxarium_studio"]) as client:
            listed = await client.list_tools()
            assert {"create_pipeline", "create_automation", "create_skill"} <= {
                tool.name for tool in listed.tools
            }
            created = await client.call_tool("create_pipeline", {"pipeline": {
                "id": "bridge-pipeline", "name": "MCP pipeline",
                "nodes": [
                    {"id": "read", "type": "core.read_text_file", "params": {"path": "notes.txt"}},
                    {"id": "write", "type": "core.write_text_file", "params": {"path": "copy.txt"}},
                ],
                "edges": [{"from": {"node": "read", "port": "text"},
                           "to": {"node": "write", "port": "text"}}],
            }})
            assert not created.isError, created
            assert ctx.pipelines.get("bridge-pipeline").edges[0].source.port == "text"
            created = await client.call_tool("create_automation", {"automation": {
                "trigger": {"kind": "path.changed", "path": "Notes", "pattern": "*.txt"},
                "action": {"kind": "pipeline.run", "pipeline_id": "bridge-pipeline"},
            }})
            assert not created.isError, created
            saved = json.loads(created.content[0].text)
            assert not ctx.automation_store.get(saved["id"]).enabled
            created = await client.call_tool("create_skill", {
                "name": "bridge-skill",
                "content": "---\nname: bridge-skill\ndescription: Write notes.\n---\n# Notes\n",
            })
            assert not created.isError, created
            read = await client.call_tool("read_skill", {"name": "bridge-skill"})
            saved = json.loads(read.content[0].text)
            written = await client.call_tool("write_skill", {
                "name": "bridge-skill", "content": saved["content"] + "More notes.\n",
                "expected_updated_at": saved["updated_at"],
            })
            assert not written.isError, written
            rejected = await client.call_tool("create_pipeline", {"pipeline": {
                "id": "bad", "name": "Bad", "nodes": [{"id": "n", "type": "missing.type"}],
            }})
            assert rejected.isError
            assert not ctx.pipelines.exists("bad")


async def test_board_delegation_and_docs_use_the_live_actor_and_limits(ctx):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        manager_id = dev.manager_id
    assert manager_id
    parent = await ctx.teams.create_task(
        team_id=TEAM, assignee_id=DEV, title="Parent", brief="Work", auto_start=False
    )
    ctx.config.teams.max_delegated_tasks_per_task = 1
    toolset = build_toolset(ctx.teams, member=dev, task=parent)
    assert toolset is not None
    async with bridge_servers(toolset.mcp_servers, 1_048_576) as servers:
        async with connect(servers["voxarium_studio"]) as client:
            listed = await client.list_tools()
            assert {"create_task", "list_team", "write_doc"} <= {t.name for t in listed.tools}
            result = await client.call_tool("create_task", {
                "assignee_id": manager_id, "title": "Technical handoff", "brief": "Review model"
            })
            assert not result.isError
            async with ctx.db.session() as session:
                child = (await session.exec(select(TeamTask).where(TeamTask.title == "Technical handoff"))).one()
            assert child.created_by == dev.id and child.assignee_id == manager_id
            assert child.delegation_depth == parent.delegation_depth + 1
            assert child.status == "todo"
            denied = await client.call_tool("create_task", {
                "assignee_id": manager_id, "title": "Beyond budget", "brief": "Must fail"
            })
            assert denied.isError and "as many tasks" in denied.content[0].text
            written = await client.call_tool("write_doc", {"path": "model.md", "content": "# Малявка"})
            assert not written.isError
            read = await client.call_tool("read_doc", {"path": "model.md"})
            assert not read.isError and "Малявка" in read.content[0].text
            invalid = await client.call_tool("write_doc", {"path": "../../escape.md", "content": "x"})
            assert invalid.isError


async def test_bridge_preserves_routing_checks(ctx):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
        outsider = TeamMember(team_id=TEAM, name="Unrelated", role_title="Artist")
        session.add(outsider)
    dev.can_assign_to = []
    toolset = build_toolset(ctx.teams, member=dev)
    async with bridge_servers(toolset.mcp_servers, 1_048_576) as servers:
        async with connect(servers["voxarium_studio"]) as client:
            result = await client.call_tool("create_task", {
                "assignee_id": outsider.id, "title": "Forbidden", "brief": "Must fail"
            })
            assert result.isError and "RoutingError" in result.content[0].text


async def test_bridge_rejects_wrong_secret_and_closes_connections_on_exit(ctx):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
    toolset = build_toolset(ctx.teams, member=dev)
    async with bridge_servers(toolset.mcp_servers, 1_048_576) as servers:
        config = servers["voxarium_studio"]
        port = int(config["args"][-1])
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x" * 64 + b"\n")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), timeout=3) == b""
        writer.close()
        await writer.wait_closed()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write((config["env"][TOKEN_ENV] + "\n").encode())
        await writer.drain()
    assert await asyncio.wait_for(reader.read(), timeout=3) == b""
    writer.close()
    await writer.wait_closed()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_bridge_cancellation_closes_listener(ctx):
    async with ctx.db.session() as session:
        dev = await session.get(TeamMember, DEV)
    toolset = build_toolset(ctx.teams, member=dev)
    ready = asyncio.Future()

    async def run():
        async with bridge_servers(toolset.mcp_servers, 1_048_576) as servers:
            ready.set_result(int(servers["voxarium_studio"]["args"][-1]))
            await asyncio.Future()

    task = asyncio.create_task(run())
    port = await asyncio.wait_for(ready, timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)

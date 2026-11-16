"""Real adapter lifecycles with fake SDK/CLI, without provider calls (§5.3.2)."""

import sys
from contextlib import AsyncExitStack, aclosing
from dataclasses import replace

import pytest

import test_qwen_backend as qwen_fakes
import test_teams as claude_fakes
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.backends import base, claude_code, qwen


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch):
    monkeypatch.setattr(base, "_LIVE_SESSIONS", {})
    monkeypatch.setattr(base, "_CONTEXT_INSPECTORS", {})


@pytest.fixture
def claude_sdk(monkeypatch):
    """Reuse the protocol fake already exercised by the Claude adapter contract tests."""
    def install(sessions):
        module, _, _ = claude_fakes._fake_sdk(sessions)
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
        monkeypatch.setattr(claude_code.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        claude_code._option_fields.cache_clear()

    yield install
    claude_code._option_fields.cache_clear()


def request(root, *, key=None):
    if key is None:
        key = base.RuntimeSessionKey(base.runtime_session_owner(root, "task", "copied-id"))
    return base.AgentRunRequest(
        member=TeamMember(team_id="team", name="Dev", role_title="Developer"),
        task=None, workdir=root, prompt="Work", system_prompt="You are Dev.",
        session_key="copied-id", runtime_key=key,
    )


def claude_turn(text):
    message = claude_fakes._AssistantMessage(text)
    # The adapter identifies SDK block variants by their public class name.
    message.content = [claude_fakes.TextBlock(text)]
    return [message, claude_fakes._ResultMessage("Done.")]


async def finish(stream):
    events = [event async for event in stream]
    assert events and events[-1].kind == "result"
    assert not any(event.kind == "error" for event in events)


async def test_claude_text_only_replace_preserves_the_active_parent(tmp_path, claude_sdk):
    claude_sdk([claude_turn("Parent working"), claude_turn("Summary")])
    parent_request = request(tmp_path)
    async with aclosing(claude_code.ClaudeCodeBackend().run(parent_request)) as parent:
        assert (await anext(parent)).kind == "text"
        control = base.live_session(parent_request.runtime_key.owner)
        assert control is not None
        # This is how compaction is constructed: replace() inherits the parent's key.
        summary_request = replace(parent_request, text_only=True, prompt="Summarise")
        assert summary_request.runtime_key is parent_request.runtime_key
        async with aclosing(claude_code.ClaudeCodeBackend().run(summary_request)) as summary:
            assert (await anext(summary)).text == "Summary"
            assert base.live_session(parent_request.runtime_key.owner) is control
            await finish(summary)
        assert base.live_session(parent_request.runtime_key.owner) is control
    assert base.live_session(parent_request.runtime_key.owner) is None


@pytest.mark.parametrize("old_ending", ["complete", "close"])
async def test_claude_stale_finally_preserves_a_running_replacement(
    tmp_path, claude_sdk, old_ending,
):
    claude_sdk([claude_turn("Old working"), claude_turn("New working")])
    old_request = request(tmp_path)
    new_request = replace(old_request, runtime_key=base.RuntimeSessionKey(old_request.runtime_key.owner))
    async with AsyncExitStack() as stack:
        old = await stack.enter_async_context(aclosing(claude_code.ClaudeCodeBackend().run(old_request)))
        new = await stack.enter_async_context(aclosing(claude_code.ClaudeCodeBackend().run(new_request)))
        await anext(old)
        old_control = base.live_session(old_request.runtime_key.owner)
        await anext(new)
        new_control = base.live_session(new_request.runtime_key.owner)
        assert old_control is not None and new_control is not None and new_control is not old_control
        if old_ending == "complete":
            await finish(old)
        else:
            await old.aclose()
        assert base.live_session(new_request.runtime_key.owner) is new_control
        await finish(new)
        assert base.live_session(new_request.runtime_key.owner) is None


async def test_claude_overlapping_projects_with_copied_ids_cleanup_independently(tmp_path, claude_sdk):
    claude_sdk([claude_turn("A working"), claude_turn("B working")])
    a, b = request(tmp_path / "a"), request(tmp_path / "b")
    assert a.session_key == b.session_key
    async with AsyncExitStack() as stack:
        stream_a = await stack.enter_async_context(aclosing(claude_code.ClaudeCodeBackend().run(a)))
        stream_b = await stack.enter_async_context(aclosing(claude_code.ClaudeCodeBackend().run(b)))
        await anext(stream_a)
        await anext(stream_b)
        control_a, control_b = base.live_session(a.runtime_key.owner), base.live_session(b.runtime_key.owner)
        assert control_a is not None and control_b is not None and control_a is not control_b
        await finish(stream_a)
        assert base.live_session(a.runtime_key.owner) is None
        assert base.live_session(b.runtime_key.owner) is control_b
        await finish(stream_b)
    assert base.live_session(b.runtime_key.owner) is None


async def start_qwen(stream, owner):
    """Pause a real CLI-backed turn once the init frame identifies its inspector."""
    while True:
        event = await anext(stream)
        assert event.kind != "error", event.text
        inspector = base.context_inspector(owner, "qwen-session")
        if inspector is not None:
            break
    assert not inspector.ready
    return inspector


@pytest.mark.parametrize("old_ending", ["complete", "close"])
async def test_qwen_stale_finally_and_text_only_preserve_the_replacement(
    tmp_path, monkeypatch, old_ending,
):
    qwen_fakes.Fake(tmp_path, monkeypatch)
    old_request = request(tmp_path)
    owner = old_request.runtime_key.owner
    new_request = replace(old_request, runtime_key=base.RuntimeSessionKey(owner))
    async with AsyncExitStack() as stack:
        old = await stack.enter_async_context(aclosing(qwen.QwenBackend().run(old_request)))
        new = await stack.enter_async_context(aclosing(qwen.QwenBackend().run(new_request)))
        old_inspector = await start_qwen(old, owner)
        new_inspector = await start_qwen(new, owner)
        assert new_inspector is not old_inspector
        if old_ending == "complete":
            await finish(old)
        else:
            await old.aclose()
        assert old_inspector.ready
        assert base.context_inspector(owner, "qwen-session") is new_inspector
        assert not new_inspector.ready
        # Text-only replace() must neither replace the inspector nor mark it ready.
        await finish(qwen.QwenBackend().run(replace(new_request, text_only=True)))
        assert base.context_inspector(owner, "qwen-session") is new_inspector
        assert not new_inspector.ready
        await finish(new)
        assert new_inspector.ready
        await finish(qwen.QwenBackend().run(replace(new_request, text_only=True)))
        assert base.context_inspector(owner, "qwen-session") is new_inspector
        assert new_inspector.ready


async def test_qwen_overlapping_projects_with_copied_ids_retain_separate_inspectors(tmp_path, monkeypatch):
    qwen_fakes.Fake(tmp_path, monkeypatch)
    roots = [tmp_path / project for project in ("a", "b")]
    for root in roots:
        root.mkdir()
    a, b = [request(root) for root in roots]
    assert a.session_key == b.session_key
    async with AsyncExitStack() as stack:
        stream_a = await stack.enter_async_context(aclosing(qwen.QwenBackend().run(a)))
        stream_b = await stack.enter_async_context(aclosing(qwen.QwenBackend().run(b)))
        inspector_a = await start_qwen(stream_a, a.runtime_key.owner)
        inspector_b = await start_qwen(stream_b, b.runtime_key.owner)
        assert inspector_a is not inspector_b
        await finish(stream_a)
        assert inspector_a.ready and not inspector_b.ready
        assert base.context_inspector(a.runtime_key.owner, "qwen-session") is inspector_a
        assert base.context_inspector(b.runtime_key.owner, "qwen-session") is inspector_b
        await finish(stream_b)
    assert inspector_b.ready
    assert base.context_inspector(a.runtime_key.owner, "qwen-session") is inspector_a
    assert base.context_inspector(b.runtime_key.owner, "qwen-session") is inspector_b


async def test_qwen_finished_inspectors_are_retained_with_a_bound(tmp_path, monkeypatch):
    qwen_fakes.Fake(tmp_path, monkeypatch)
    monkeypatch.setattr(base, "_MAX_CONTEXT_INSPECTORS", 2)
    requests = []
    for name in ("oldest", "middle", "latest"):
        root = tmp_path / name
        root.mkdir()
        req = request(root)
        requests.append(req)
        await finish(qwen.QwenBackend().run(req))
    assert base.context_inspector(requests[0].runtime_key.owner, "qwen-session") is None
    for req in requests[1:]:
        assert base.context_inspector(req.runtime_key.owner, "qwen-session").ready
    assert len(base._CONTEXT_INSPECTORS) == 2

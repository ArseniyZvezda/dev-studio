"""VS-184: Auto chats resolve before any runtime or Producer compaction."""
import pytest

from voxarium_studio.core.application_config import ApplicationConfig, save_application_config
from voxarium_studio.models.teams import TeamMember
from voxarium_studio.teams.backends.base import AgentEvent, register_backend
from voxarium_studio.teams.chat import UserContext
from voxarium_studio.teams.effort_solver_runner import EffortSolution


class ChatBackend:
    requests = []
    name = 'orchestrator_chat'

    def is_available(self):
        return True, ''

    async def run(self, request):
        self.requests.append(request)
        yield AgentEvent(kind='result', text='Done', session_id='auto-chat-session',
                         data={'num_turns': 1}, is_final=True)


register_backend(ChatBackend.name, ChatBackend)


@pytest.mark.parametrize('fixed_effort', [None, 'low'])
@pytest.mark.parametrize('producer_mode', [False, True])
async def test_auto_chat_reselects_model_and_keeps_policy(ctx, monkeypatch, wait_for, fixed_effort, producer_mode):
    import voxarium_studio.teams.effort_solver_runner as evaluator

    ChatBackend.requests = []
    strengths = iter([1, 3, 3])
    calls = []

    async def solve(member, title, brief, **kwargs):
        calls.append((title, brief))
        return EffortSolution(model=next(strengths), effort=4, effort_level='xhigh', usage={})

    monkeypatch.setattr(evaluator, 'solve_effort', solve)
    compacted_models = []

    async def compact(chat_id, backend, request, **kwargs):
        compacted_models.append(request.model)
        assert request.member.model != 'auto'
        return False

    monkeypatch.setattr(ctx.chats.producer, 'compact', compact)
    config = ApplicationConfig()
    config.orchestrator_models[ChatBackend.name] = {'1': 'small', '2': 'mid', '3': 'strong'}
    save_application_config(config)
    member_id = 'team_default__studio-producer' if producer_mode else 'team_default__dev'
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, member_id)
        member.agent_backend = ChatBackend.name
        member.model = 'auto'
        member.effort = fixed_effort
        session.add(member)
    chat = await ctx.chats.create_chat(member_id, producer_mode=producer_mode)
    for text in ['routine edit', 'hard architecture', 'continue']:
        await ctx.chats.send(chat.id, text, UserContext(page='Tasks'))
        await wait_for(chat.id)
        assert (await ctx.chats.get_chat(chat.id)).error is None
    assert [r.model for r in ChatBackend.requests] == ['small', 'strong', 'strong']
    assert [r.resume_session_id for r in ChatBackend.requests] == [None, None, 'auto-chat-session']
    assert all(r.effort == (fixed_effort or 'xhigh') for r in ChatBackend.requests)
    assert len(calls) == 3
    assert compacted_models == (['small', 'strong', 'strong'] if producer_mode else [])
    async with ctx.db.session() as session:
        assert (await session.get(TeamMember, member_id)).model == 'auto'


async def test_auto_chat_failure_does_not_invoke_runtime(ctx, monkeypatch, wait_for):
    import voxarium_studio.teams.effort_solver_runner as evaluator

    ChatBackend.requests = []

    async def fail(*args, **kwargs):
        raise ValueError('invalid Orchestrator response')

    monkeypatch.setattr(evaluator, 'solve_effort', fail)
    async with ctx.db.session() as session:
        member = await session.get(TeamMember, 'team_default__dev')
        member.agent_backend = ChatBackend.name
        member.model = 'auto'
        session.add(member)
    chat = await ctx.chats.create_chat('team_default__dev')
    await ctx.chats.send(chat.id, 'work', UserContext(page='Tasks'))
    await wait_for(chat.id)
    assert 'invalid Orchestrator response' in (await ctx.chats.get_chat(chat.id)).error
    assert not ChatBackend.requests

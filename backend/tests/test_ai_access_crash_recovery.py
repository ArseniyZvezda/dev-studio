"""Crash-window tests for the executor; never launch an actual provider."""
from __future__ import annotations

import pytest

from test_ai_access_executor import Backend, acp, forward, handle, new_chat  # noqa: F401
from voxarium_studio.ai_access import ledger, protocol as p
from voxarium_studio.ai_access.executor import Executor, recover_project


@pytest.mark.parametrize('crash_before_backend', [True, False])
async def test_lost_dispatch_receipt_is_never_replayed(acp, monkeypatch, crash_before_backend):
    ctx, _, grant, *_ = acp
    chat = await new_chat(acp)
    message = forward(acp, p.Operation.CHAT_SEND, {'resource_id': chat, 'text': 'one intent'}, revision=1)
    key = ledger.LedgerKey.of(grant.target, grant.grantee_registration_id, message.request.command_id)
    if crash_before_backend:
        def interruption(*args, **kwargs):
            raise RuntimeError('fault after durable dispatching marker')
        monkeypatch.setattr(ctx.chats, 'remote_dispatch_chat', interruption)
        with pytest.raises(RuntimeError, match='fault after durable'):
            await handle(acp, message)
    else:
        async def lost_terminal(*args, **kwargs):
            raise RuntimeError('fault before durable terminal receipt')
        with monkeypatch.context() as patch:
            patch.setattr(ledger, 'mark_terminal', lost_terminal)
            assert (await handle(acp, message)).state == p.CommandState.ACCEPTED
            with pytest.raises(RuntimeError, match='fault before durable terminal'):
                await ctx.jobs.get(chat).task
    await recover_project(ctx.db)
    assert (await ledger.lookup(ctx.db, key)).state == p.CommandState.OUTCOME_UNKNOWN
    restarted = Executor(lambda target: ctx if target == grant.target else None)
    result = await restarted.handle(message, executor_epoch='f2b4dc16-86da-4616-a17f-2866a4320984',
                                    live=lambda: True)
    assert result.state == p.CommandState.OUTCOME_UNKNOWN
    assert Backend.calls == (0 if crash_before_backend else 1)

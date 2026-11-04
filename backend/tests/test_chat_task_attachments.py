from uuid import uuid4

from voxarium_studio.models.teams import TeamTask
from voxarium_studio.teams.chat import UserContext
from test_chats import chats, RecordingBackend, turn_done, DEV, TEAM


def upload_record(ctx, name='reference.png'):
    folder = 'upload_' + uuid4().hex
    record = ctx.teams.attachments.add(folder, name, b'example')
    return f"{folder}/{record['name']}"


async def test_chat_attachment_only_reaches_agent_and_history(chats):
    reference = upload_record(chats)
    chat = await chats.chats.create_chat(DEV)
    message = await chats.chats.send(chat.id, '', UserContext(), attachments=[reference])
    await turn_done(chats, chat.id)
    assert message.data['attachments'][0]['reference'] == reference
    request = RecordingBackend.requests[-1]
    assert request.images[0]['media_type'] == 'image/png'
    assert str(chats.paths.attachments) in request.prompt
    assert any('attachments' in tool for tool in request.extra_allowed_tools)


async def test_task_attachments_persist_and_open_on_first_and_resumed_turn(ctx):
    reference = upload_record(ctx)
    task = await ctx.teams.create_task(team_id=TEAM, assignee_id=DEV, title='Reference', brief='Draw this', attachments=[reference])
    async with ctx.db.session() as session:
        saved = await session.get(TeamTask, task.id)
        assert saved.attachments[0]['reference'] == reference
        prompt, images = await ctx.teams._opening_turn(session, saved)
        assert 'Draw this' in prompt and reference.split('/')[1] in prompt
        assert images[0]['media_type'] == 'image/png'
        saved.agent_session_id = 'previous'
        prompt, images = await ctx.teams._opening_turn(session, saved)
        assert reference.split('/')[1] in prompt and images
    await ctx.teams.update_task(task.id, {'attachments': []})
    assert (await ctx.teams.find_task(task.id)).attachments == []


def test_upload_download_and_invalid_references(client):
    response = client.post('/api/attachments', files={'file': ('notes.html', b'<script>bad</script>', 'text/html')})
    assert response.status_code == 201
    reference = response.json()['reference']
    download = client.get('/api/attachments/' + reference)
    assert download.content == b'<script>bad</script>'
    assert download.headers['content-disposition'].startswith('attachment')
    assert download.headers['x-content-type-options'] == 'nosniff'
    invalid = client.post(f'/api/teams/{TEAM}/tasks', json={'assignee_id': DEV, 'title': 'Invalid', 'attachments': ['../../secret']})
    assert invalid.status_code == 400
    missing = client.post(f'/api/teams/{TEAM}/tasks', json={'assignee_id': DEV, 'title': 'Missing', 'attachments': ['upload_' + 'a'*32 + '/missing.png']})
    assert missing.status_code == 400


def test_upload_limit(client, monkeypatch):
    monkeypatch.setattr('voxarium_studio.api.attachments.MAX_ATTACHMENT_BYTES', 3)
    assert client.post('/api/attachments', files={'file': ('big.txt', b'1234')}).status_code == 413

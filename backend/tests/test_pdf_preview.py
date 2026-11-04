"""PDF uses the ordinary plugin seam, including its authenticated asset paths (§6.2)."""
import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize('name', ['guide.pdf', 'Документ.PDF'])
def test_pdf_preview_and_authenticated_bytes(token_client: TestClient, name: str) -> None:
    client = token_client
    content = b'%PDF-1.4\n%%EOF\n'
    (client.app.state.ctx.paths.root / name).write_bytes(content)
    headers = {'Authorization': 'Bearer t0ken'}
    response = client.get('/api/explorer/file', params={'path': name}, headers=headers)
    assert response.status_code == 200
    previewer = response.json()['previewer']
    assert previewer['plugin_id'] == 'pdf_preview'
    assert client.get(previewer['component_url']).status_code == 401
    assert client.get(previewer['component_url'], headers=headers).status_code == 200
    assert client.get('/api/explorer/raw', params={'path': name}).status_code == 401
    raw = client.get('/api/explorer/raw', params={'path': name}, headers=headers)
    assert raw.status_code == 200
    assert raw.content == content

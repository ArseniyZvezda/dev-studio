"""VS-184: Orchestrator mapping is validated and saved only globally (§10.11)."""
import pytest
import yaml
from pydantic import ValidationError

from voxarium_studio.api.project import ProjectSettingsUpdate
from voxarium_studio.core.application_config import application_config_file


def test_mapping_global_round_trip_without_project_write(client):
    paths = client.app.state.ctx.paths
    before = paths.config_file.read_bytes()
    mapping = client.get('/api/settings/application').json()['orchestrator_models']
    mapping['codex'] = {'1': 'gpt-6.1-terra', '2': 'gpt-6.1-sol', '3': 'gpt-6-astra'}
    response = client.put('/api/settings/application', json={'orchestrator_models': mapping})
    assert response.status_code == 200, response.text
    assert response.json()['orchestrator_models'] == mapping
    assert client.get('/api/settings/application').json()['orchestrator_models'] == mapping
    assert client.app.state.ctx.config.orchestrator_models == mapping
    assert paths.config_file.read_bytes() == before
    assert yaml.safe_load(application_config_file().read_text(encoding='utf-8'))['orchestrator_models'] == mapping


@pytest.mark.parametrize('tiers', [
    {}, {'1': 'a', '2': 'b'}, {'1': 'a', '2': 'b', '3': 'auto'},
    {'1': 'a', '2': 'b', '3': ''}, {'1': 'a', '2': 'b', '3': 'c', '4': 'd'},
    {'1': 'a', '2': 'b', '3': 3},
])
def test_mapping_invalid_cells_leave_storage_unchanged(client, tiers):
    before = application_config_file().read_bytes()
    response = client.put('/api/settings/application', json={'orchestrator_models': {'codex': tiers}})
    assert response.status_code == 422
    assert application_config_file().read_bytes() == before


def test_project_settings_reject_orchestrator_mapping():
    with pytest.raises(ValidationError):
        ProjectSettingsUpdate.model_validate({'orchestrator_models': {}})

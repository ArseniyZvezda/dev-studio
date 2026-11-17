"""Strict HTTP settings DTOs, permissive historical disk models (§5.2.1).

VS-101 / foundation-reaudit A. Run with an isolated pytest basetemp/home; the
task's no-socket harness roots these under .voxarium_studio/temp/TASK-101.
The F1 helpers supply the real in-process app, exact redacted error contract,
home/live-state snapshots, and log checks through teardown. No server is started.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from voxarium_studio.api import project as settings_api
from voxarium_studio.core.application_config import (
    application_config_file,
    load_application_config,
)
from voxarium_studio.core.config import (
    AppConfig,
    ApplicationThemeConfig,
    ConcurrencyConfig,
    DocsConfig,
    TeamsConfig,
    ThemeConfig,
    UpdatesConfig,
    load_config,
)
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.prompts import AgentPromptsConfig

# Explicit fixture imports are intentional: pytest does not implicitly enable
# another test module's isolation fixtures when its helpers are imported.
from test_settings_error_redaction import (
    REQUEST_MARKER,
    _assert_unchanged,
    _assert_validation,
    _settings_url,
    _snapshot,
    http,
    isolated_environment,
)


EXTRA = {"token": {"nest": [REQUEST_MARKER]}}

# All nested objects accepted by each boundary, not just the six audit repros.
NESTED_SECTIONS = [
    ("legacy", "theme"), ("legacy", "teams"),
    ("legacy", "concurrency"), ("legacy", "docs"),
    ("legacy", "updates"),
    ("application", "theme"), ("application", "updates"),
    ("scoped", "theme"), ("scoped", "teams"),
    ("scoped", "concurrency"), ("scoped", "docs"),
]


def _assert_rejected_without_mutation(http, project, studio_home, caplog, monkeypatch, kind, payload):
    """Refusal must precede every persistence/reconfiguration boundary."""
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    calls = []
    with monkeypatch.context() as patch:
        for name in ("save_config", "save_application_config", "_configure_application"):
            spy = AsyncMock() if name == "_configure_application" else Mock()
            patch.setattr(settings_api, name, spy)
            calls.append(spy)
        for ctx in http.app.state.project_contexts.values():
            for owner, name, asynchronous in (
                (ctx.jobs, "update_limits", False),
                (ctx.updates, "configure", True),
                (ctx.teams, "sync_agent_definitions", True),
                (ctx.sync, "reconfigure", True),
                (ctx, "reload_plugins", True),
                (ctx, "reload_config", False),
            ):
                spy = AsyncMock() if asynchronous else Mock()
                patch.setattr(owner, name, spy)
                calls.append(spy)
        sibling = {"language": "ru"} if kind == "application" else {
            "project_name": "must-not-save",
        }
        _assert_validation(http.put(url, json={**sibling, **payload}), caplog)
        for spy in calls:
            spy.assert_not_called()
        _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize(("kind", "section"), [
    ("legacy", "theme"), ("legacy", "teams"),
    ("legacy", "concurrency"), ("legacy", "docs"),
    ("application", "updates"), ("scoped", "docs"),
], ids=["legacy-theme", "legacy-teams", "legacy-concurrency", "legacy-docs",
        "application-updates", "scoped-docs"])
def test_six_audit_nested_unknown_reproductions_are_safe_and_atomic(
    http, project, studio_home, caplog, monkeypatch, kind, section,
):
    _assert_rejected_without_mutation(
        http, project, studio_home, caplog, monkeypatch, kind, {section: EXTRA},
    )


@pytest.mark.parametrize(("kind", "section"), NESTED_SECTIONS)
@pytest.mark.parametrize("extra_key", ["token", REQUEST_MARKER], ids=["unknown-value", "unknown-name"])
def test_every_legal_nested_dto_rejects_unknown_values_and_names(
    http, project, studio_home, caplog, monkeypatch, kind, section, extra_key,
):
    _assert_rejected_without_mutation(
        http, project, studio_home, caplog, monkeypatch, kind,
        {section: {extra_key: {"nest": [REQUEST_MARKER]}}},
    )


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
@pytest.mark.parametrize("extra_key", ["token", REQUEST_MARKER])
def test_deeper_prompts_model_is_strict_even_with_valid_prompt_and_team_siblings(
    http, project, studio_home, caplog, monkeypatch, kind, extra_key,
):
    _assert_rejected_without_mutation(
        http, project, studio_home, caplog, monkeypatch, kind,
        {"teams": {"max_turns": 71, "prompts": {
            "identity": "must-not-save", extra_key: {"nest": [REQUEST_MARKER]},
        }}},
    )


@pytest.mark.parametrize(("kind", "payload"), [
    (kind, payload)
    for kind in ("legacy", "scoped")
    for payload in (
        {"concurrency": {"cpu": 0}},
        {"concurrency": {"agents": 0}},
        {"teams": {"permission_mode": REQUEST_MARKER}},
        {"teams": {"max_turns": 1001}},
        {"teams": {"memory_index_chars": 499}},
        {"teams": {"max_message_bytes": 65535}},
        {"teams": {"prompts": {"identity": {"nest": [REQUEST_MARKER]}}}},
        {"docs": {"font_size": 29}},
    )
] + [
    (kind, payload)
    for kind in ("legacy", "application")
    for payload in (
        {"theme": {"mode": REQUEST_MARKER}},
        {"theme": {"text_size": 19}},
        {"theme": {"node_tint": 61}},
        {"updates": {"check_interval_minutes": 4}},
        {"theme": {"custom_tokens": {REQUEST_MARKER: {"nest": [REQUEST_MARKER]}}}},
    )
])
def test_inherited_bounds_literals_and_validators_remain_redacted_and_atomic(
    http, project, studio_home, caplog, monkeypatch, kind, payload,
):
    _assert_rejected_without_mutation(
        http, project, studio_home, caplog, monkeypatch, kind, payload,
    )


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
def test_concurrency_by_kind_is_not_a_field_in_the_current_schema(
    http, project, studio_home, caplog, monkeypatch, kind,
):
    # The actual model has gpu/cpu/io/agents, not by_kind. Do not invent an API
    # or bless discarded data while testing strictness. custom_tokens below is
    # the legal arbitrary-key map on the existing settings boundary.
    assert set(ConcurrencyConfig.model_fields) == {"gpu", "cpu", "io", "agents"}
    _assert_rejected_without_mutation(
        http, project, studio_home, caplog, monkeypatch, kind,
        {"concurrency": {"by_kind": {"arbitrary-kind": 3}}},
    )


@pytest.mark.parametrize("kind", ["legacy", "application"])
def test_free_form_theme_token_keys_are_legal_but_values_still_have_string_type(http, project, kind):
    url = _settings_url(http, project, kind)
    tokens = {"--fixture-color": "#123456", "token": "ordinary CSS text", "arbitrary.key/☃": "7"}
    response = http.put(url, json={"theme": {"custom_tokens": tokens}})
    assert response.status_code == 200, response.text
    assert response.json()["theme"]["custom_tokens"] == tokens
    assert load_application_config().theme.custom_tokens == tokens


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
def test_valid_project_partial_updates_preserve_nested_siblings_and_global_file(http, project, kind):
    url = _settings_url(http, project, kind)
    global_before = application_config_file().read_bytes()
    baseline = {
        "theme": {"accent": "#123456"},
        "concurrency": {"gpu": 2, "cpu": 6, "io": 9, "agents": 3},
        "teams": {"max_turns": 71, "max_delegation_depth": 4, "memory_enabled": False,
                  "prompts": {"identity": "Fixture identity {name}", "subagents": "Keep this prompt"}},
        "docs": {"font_size": 20},
    }
    response = http.put(url, json=baseline)
    assert response.status_code == 200, response.text
    before = response.json()
    config_object = http.app.state.ctx.config
    response = http.put(url, json={
        "project_name": "Accepted fixture", "concurrency": {"cpu": 7},
        "teams": {"max_turns": 72, "prompts": {"identity": None}},
        "docs": {"font_size": None},
    })
    assert response.status_code == 200, response.text
    after = response.json()
    expected = deepcopy(before)
    expected["project_name"] = "Accepted fixture"
    expected["concurrency"]["cpu"] = 7
    expected["teams"]["max_turns"] = 72
    expected["teams"]["prompts"]["identity"] = None
    expected["docs"]["font_size"] = None
    assert after == expected
    assert http.app.state.ctx.config is config_object
    assert http.app.state.ctx.jobs.limits == {"gpu": 2, "cpu": 7, "io": 9, "agent": 3}
    assert application_config_file().read_bytes() == global_before
    stored = yaml.safe_load(ProjectPaths.for_project(project).config_file.read_text(encoding="utf-8"))
    assert stored["theme"] == {"accent": "#123456"}
    assert stored["concurrency"] == expected["concurrency"]
    assert stored["teams"]["prompts"]["subagents"] == "Keep this prompt"
    assert "identity" not in stored["teams"]["prompts"]
    assert "language" not in stored and "updates" not in stored
    assert load_config(ProjectPaths.for_project(project)).model_dump(mode="json") == after


@pytest.mark.parametrize("kind", ["legacy", "application"])
def test_global_partial_updates_keep_opaque_f2_data_private_and_preserved(http, project, kind):
    url = _settings_url(http, project, kind)
    path = application_config_file()
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["future_section"] = {"future_flag": {"nest": [REQUEST_MARKER]}}
    document["theme"]["future_theme_flag"] = {"nest": [REQUEST_MARKER]}
    document["updates"]["future_update_flag"] = {"nest": [REQUEST_MARKER]}
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    project_before = ProjectPaths.for_project(project).config_file.read_bytes()
    first = http.put(url, json={
        "language": "ru", "theme": {"mode": "light", "text_size": 17, "node_tint": 22},
        "updates": {"enabled": False, "check_interval_minutes": 25},
    })
    assert first.status_code == 200, first.text
    second = http.put(url, json={"theme": {"text_size": None}, "updates": {"check_interval_minutes": 30}})
    assert second.status_code == 200, second.text
    expected = first.json()
    expected["theme"]["text_size"] = None
    expected["updates"]["check_interval_minutes"] = 30
    assert second.json() == expected
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["future_section"] == document["future_section"]
    assert saved["theme"]["future_theme_flag"] == document["theme"]["future_theme_flag"]
    assert saved["updates"]["future_update_flag"] == document["updates"]["future_update_flag"]
    assert ProjectPaths.for_project(project).config_file.read_bytes() == project_before
    for response in (first, second, http.get("/api/settings"), http.get("/api/settings/application")):
        assert REQUEST_MARKER not in response.text
        assert "future_section" not in response.text
        assert "future_theme_flag" not in response.text
        assert "future_update_flag" not in response.text


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
def test_all_prompt_fields_remain_legal_and_empty_objects_are_partial_patches(http, project, kind):
    url = _settings_url(http, project, kind)
    prompts = {key: f"Fixture override for {key}" for key in AgentPromptsConfig.model_fields}
    response = http.put(url, json={"teams": {"prompts": prompts}})
    assert response.status_code == 200, response.text
    before = response.json()
    assert before["teams"]["prompts"] == prompts
    for payload in ({}, {"theme": {}, "teams": {"prompts": {}}, "concurrency": {}, "docs": {}}):
        response = http.put(url, json=payload)
        assert response.status_code == 200, response.text
        assert response.json() == before
    response = http.put(url, json={"teams": {"prompts": {"identity": ""}}})
    assert response.status_code == 200, response.text
    assert response.json()["teams"]["prompts"] == {**prompts, "identity": ""}


@pytest.mark.parametrize(("request_model", "field", "disk_model"), [
    (settings_api.SettingsUpdate, "theme", ThemeConfig),
    (settings_api.SettingsUpdate, "concurrency", ConcurrencyConfig),
    (settings_api.SettingsUpdate, "teams", TeamsConfig),
    (settings_api.SettingsUpdate, "docs", DocsConfig),
    (settings_api.SettingsUpdate, "updates", UpdatesConfig),
    (settings_api.ApplicationSettingsUpdate, "theme", ApplicationThemeConfig),
    (settings_api.ApplicationSettingsUpdate, "updates", UpdatesConfig),
    (settings_api.ProjectSettingsUpdate, "concurrency", ConcurrencyConfig),
    (settings_api.ProjectSettingsUpdate, "teams", TeamsConfig),
    (settings_api.ProjectSettingsUpdate, "docs", DocsConfig),
])
def test_api_nested_models_inherit_fields_constraints_and_defaults(request_model, field, disk_model):
    nested = getattr(request_model.model_validate({field: {}}), field)
    assert isinstance(nested, disk_model)
    assert nested.model_fields_set == set()
    assert set(type(nested).model_fields) == set(disk_model.model_fields)
    for name, disk_field in disk_model.model_fields.items():
        api_field = type(nested).model_fields[name]
        assert api_field.metadata == disk_field.metadata
        if name != "prompts":
            assert api_field.annotation == disk_field.annotation
        # conftest's no_result_follow_up intentionally changes the disk class
        # default after API subclasses have already been built at import time.
        if name == "prompts":
            assert nested.prompts.model_dump() == AgentPromptsConfig().model_dump()
            assert nested.prompts.model_fields_set == set()
        elif name != "result_follow_up":
            assert getattr(nested, name) == getattr(disk_model(), name)
    assert request_model.model_validate({field: {}}).model_dump(exclude_unset=True) == {field: {}}


@pytest.mark.parametrize("model", [ThemeConfig, ConcurrencyConfig, TeamsConfig, DocsConfig, UpdatesConfig, AgentPromptsConfig])
def test_historical_disk_leaf_models_still_ignore_unknown_fields(model):
    instance = model.model_validate(EXTRA)
    assert "token" not in instance.model_dump()
    assert instance.model_dump() == model().model_dump()


def test_historical_project_disk_loading_is_not_made_strict(project):
    paths = ProjectPaths.for_project(project)
    document = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    for section in ("theme", "concurrency", "teams", "docs", "updates"):
        document.setdefault(section, {}).update(deepcopy(EXTRA))
    document["teams"].setdefault("prompts", {}).update(deepcopy(EXTRA))
    paths.config_file.write_text(yaml.safe_dump(document), encoding="utf-8")
    original = paths.config_file.read_bytes()
    # Raw disk model and effective loader are both deliberately more permissive.
    parsed = AppConfig.model_validate(document)
    effective = load_config(paths)
    assert "token" not in parsed.theme.model_dump()
    assert "token" not in effective.teams.prompts.model_dump()
    assert paths.config_file.read_bytes() == original

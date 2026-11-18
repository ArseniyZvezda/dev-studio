"""Application preferences never leak into project config or overwrite its accent (§5.2)."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from voxarium_studio.core.application_config import (
    application_config_file,
    load_application_config,
)
from voxarium_studio.api.project import ApplicationSettingsUpdate, update_application_settings
from voxarium_studio.cli import app as cli_app
from voxarium_studio.core.config import load_config, save_config
from voxarium_studio.core.context import AppContext
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import ensure_project, init_project
from voxarium_studio.core.workspace import initialise_workspace, open_project


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_orchestrator_default_model_mapping_and_fresh_global_resolution():
    from voxarium_studio.core.application_config import ApplicationConfig, save_application_config
    from voxarium_studio.core.config import AppConfig, default_orchestrator_models
    from voxarium_studio.teams.effort_solver import resolve_model

    defaults = default_orchestrator_models()
    assert defaults["claude_code"] == {"1": "claude-haiku-4-5", "2": "claude-opus-5-5", "3": "claude-fable-5-2"}
    assert defaults["codex"] == {"1": "gpt-6.1-terra", "2": "gpt-6.1-sol", "3": "gpt-6-astra"}
    for backend in defaults:
        for strength in (1, 2, 3):
            assert resolve_model(backend, strength) == defaults[backend][str(strength)]
    effective = AppConfig()
    application = ApplicationConfig()
    application.orchestrator_models["codex"]["1"] = "custom-terra"
    save_application_config(application)
    assert resolve_model("codex", 1) == "custom-terra"
    assert resolve_model("codex", 1, effective) == "gpt-6.1-terra"
    application.orchestrator_models["codex"]["1"] = "replacement-terra"
    save_application_config(application)
    assert resolve_model("codex", 1) == "replacement-terra"
    for strength in (0, 4, True, "1", 1.0):
        with pytest.raises(ValueError):
            resolve_model("codex", strength)
    with pytest.raises(ValueError, match="No Orchestrator model mapping"):
        resolve_model("missing", 1)


@pytest.mark.parametrize("mutation", [
    lambda table: table.pop("codex"),
    lambda table: table["codex"].pop("2"),
    lambda table: table["codex"].update({"4": "future-model"}),
    lambda table: table["codex"].update({"1": "auto"}),
    lambda table: table["codex"].update({"1": "AUTO"}),
    lambda table: table["codex"].update({"1": "default"}),
    lambda table: table["codex"].update({"1": ""}),
    lambda table: table["codex"].update({"1": "  "}),
    lambda table: table["codex"].update({"1": 123}),
])
def test_orchestrator_model_mapping_requires_complete_concrete_columns(mutation):
    from voxarium_studio.core.application_config import ApplicationConfig
    from voxarium_studio.core.config import AppConfig, default_orchestrator_models
    mapping = default_orchestrator_models()
    mutation(mapping)
    for model in (ApplicationConfig, AppConfig, ApplicationSettingsUpdate):
        with pytest.raises(ValidationError):
            model(orchestrator_models=mapping)


def test_orchestrator_mapping_is_global_only_and_never_seeded_from_a_project(project):
    from voxarium_studio.core.application_config import ApplicationConfig, save_application_config
    from voxarium_studio.core.config import default_orchestrator_models
    from voxarium_studio.teams.effort_solver import resolve_model
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text("orchestrator_models: malicious-invalid-project-data\n", encoding="utf-8")
    effective = load_config(paths)
    assert effective.orchestrator_models == default_orchestrator_models()
    global_config = ApplicationConfig()
    global_config.orchestrator_models["codex"]["2"] = "custom-sol"
    save_application_config(global_config)
    effective = load_config(paths)
    assert resolve_model("codex", 2, effective) == "custom-sol"
    save_config(paths, effective)
    assert "orchestrator_models" not in read_yaml(paths.config_file)
    assert read_yaml(application_config_file())["orchestrator_models"]["codex"]["2"] == "custom-sol"
    # Effective objects must not share mutable nested columns with other projects.
    effective.orchestrator_models["codex"]["2"] = "local-mutation"
    assert load_config(paths).orchestrator_models["codex"]["2"] == "custom-sol"


def test_orchestrator_mapping_save_replaces_removed_custom_backend():
    from voxarium_studio.core.application_config import ApplicationConfig, save_application_config
    config = ApplicationConfig()
    config.orchestrator_models["custom"] = {"1": "small", "2": "medium", "3": "large"}
    save_application_config(config)
    assert "custom" in load_application_config().orchestrator_models
    config.orchestrator_models.pop("custom")
    save_application_config(config)
    assert "custom" not in load_application_config().orchestrator_models


def test_global_orchestrator_mapping_api_patch_is_atomic_and_project_patch_refuses_it(client, project):
    from voxarium_studio.core.config import default_orchestrator_models
    table = default_orchestrator_models()
    table["codex"]["1"] = "my-concrete-model"
    result = client.put("/api/settings/application", json={"orchestrator_models": table})
    assert result.status_code == 200, result.text
    assert load_application_config().orchestrator_models == table
    paths = ProjectPaths.for_project(project)
    assert "orchestrator_models" not in read_yaml(paths.config_file)
    before = application_config_file().read_bytes()
    table["codex"]["1"] = "auto"
    invalid = client.put("/api/settings/application", json={"orchestrator_models": table})
    assert invalid.status_code in (400, 422)
    assert application_config_file().read_bytes() == before
    project_id = next(iter(client.app.state.project_registry))
    rejected = client.put(f"/api/projects/{project_id}/settings", json={"orchestrator_models": default_orchestrator_models()})
    assert rejected.status_code in (400, 422)


def test_first_legacy_project_seeds_global_preferences_only_once(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.mkdir()
    paths = init_project(first, with_samples=False)
    paths.config_file.write_text(
        "theme:\n  mode: light\n  accent: '#112233'\n  text_size: 16\n"
        "explorer:\n  show_data_dir: true\n  ignore: [build]\n"
        "updates:\n  enabled: false\nnode_cache_enabled: false\nproject_code: ABC\n",
        encoding="utf-8",
    )

    config = load_config(paths, migrate_application=True)
    assert (config.theme.mode, config.theme.accent) == ("light", "#112233")
    global_file = application_config_file()
    global_bytes = global_file.read_bytes()
    saved = read_yaml(global_file)
    assert saved["theme"]["text_size"] == 16
    assert "accent" not in saved["theme"]
    assert saved["show_data_dir"] is True
    assert saved["updates"]["enabled"] is False
    assert saved["node_cache_enabled"] is False
    assert "project_code" not in saved

    second = tmp_path / "second"
    second.mkdir()
    other = init_project(second, with_samples=False)
    other.config_file.write_text(
        "theme:\n  mode: dark\n  accent: '#445566'\n  text_size: 12\n"
        "project_code: XYZ\nexplorer:\n  ignore: [custom]\n",
        encoding="utf-8",
    )
    other_config = load_config(other)
    assert (other_config.theme.mode, other_config.theme.text_size) == ("light", 16)
    assert other_config.theme.accent == "#445566"
    assert other_config.project_code == "XYZ"
    assert other_config.explorer.ignore == ["custom"]
    assert global_file.read_bytes() == global_bytes

    save_config(paths, config)
    project_data = read_yaml(paths.config_file)
    assert project_data["theme"] == {"accent": "#112233"}
    assert project_data["explorer"] == {"ignore": ["build"]}
    assert project_data["project_code"] == "ABC"
    for key in ("updates", "language", "respond_language", "node_cache_enabled"):
        assert key not in project_data
    assert load_config(paths).theme.mode == "light"


def test_diagnostic_reads_and_cli_info_do_not_migrate_application_preferences(project: Path) -> None:
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text("theme:\n  mode: light\n  accent: '#123456'\n", encoding="utf-8")
    assert load_config(paths).theme.mode == "light"
    assert not application_config_file().exists()
    result = CliRunner().invoke(cli_app, ["info", str(project)])
    assert result.exit_code == 0, result.output
    assert not application_config_file().exists(), "info is read-only, not a first project open"

    _, opened = ensure_project(project)
    assert opened.theme.mode == "light"
    assert read_yaml(application_config_file())["theme"]["mode"] == "light"


@pytest.mark.parametrize("contents", [None, "not a mapping", "theme: [unterminated"])
def test_missing_or_unreadable_project_cannot_suppress_later_migration(
    tmp_path: Path, contents: str | None,
) -> None:
    paths = ProjectPaths.for_project(tmp_path / "missing-or-unreadable")
    paths.data.mkdir(parents=True)
    if contents is not None:
        paths.config_file.write_text(contents, encoding="utf-8")
    assert load_config(paths, migrate_application=True).theme.mode == "dark"
    assert not application_config_file().exists()

    valid = ProjectPaths.for_project(tmp_path / "valid-legacy")
    valid.data.mkdir(parents=True)
    valid.config_file.write_text("theme:\n  mode: light\n", encoding="utf-8")
    _, opened = ensure_project(valid.root)
    assert opened.theme.mode == "light"
    assert read_yaml(application_config_file())["theme"]["mode"] == "light"


def test_existing_application_settings_ignore_invalid_legacy_global_fields_before_validation(
    project: Path,
) -> None:
    global_file = application_config_file()
    global_file.write_text("theme:\n  mode: light\n  text_size: 17\nlanguage: ru\n", encoding="utf-8")
    global_before = global_file.read_bytes()
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text(
        "theme:\n  mode: invalid\n  text_size: 999\n  accent: '#123456'\n"
        "updates:\n  check_interval_minutes: -10\n"
        "language: invalid\nnode_cache_enabled: invalid\n"
        "explorer:\n  show_data_dir: invalid\n  ignore: [build]\n"
        "concurrency:\n  agents: 3\nproject_code: ABC\n",
        encoding="utf-8",
    )
    config = load_config(paths, migrate_application=True)
    assert (config.theme.mode, config.theme.text_size, config.language) == ("light", 17, "ru")
    assert config.theme.accent == "#123456"
    assert config.explorer.ignore == ["build"]
    assert config.concurrency.agents == 3
    assert config.project_code == "ABC"
    assert global_file.read_bytes() == global_before
    save_config(paths, config)
    assert read_yaml(paths.config_file)["theme"] == {"accent": "#123456"}

    # Project-owned values still undergo normal validation.
    paths.config_file.write_text("concurrency:\n  agents: 0\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(paths)


def test_global_save_changes_user_file_without_writing_project(client: TestClient) -> None:
    paths = client.app.state.ctx.paths
    project_before = paths.config_file.read_bytes()
    accent = client.get("/api/settings").json()["theme"]["accent"]
    response = client.put("/api/settings/application", json={
        "theme": {"mode": "light", "mono_font": "fira", "text_size": 17},
        "language": "ru", "show_data_dir": True, "node_cache_enabled": False,
        "updates": {"enabled": False, "check_interval_minutes": 60},
    })
    assert response.status_code == 200
    config = response.json()
    assert config["theme"]["accent"] == accent
    assert config["language"] == "ru"
    assert config["explorer"]["show_data_dir"] is True
    assert paths.config_file.read_bytes() == project_before
    saved = read_yaml(application_config_file())
    assert saved["theme"]["mono_font"] == "fira"
    assert "accent" not in saved["theme"]
    assert saved["language"] == "ru"
    assert load_config(paths).theme.text_size == 17
    assert load_config(paths).node_cache_enabled is False
    assert client.get("/api/project").json()["config"]["language"] == "ru"


def test_response_language_is_free_text_and_independent_of_ui(client: TestClient) -> None:
    paths = client.app.state.ctx.paths
    assert client.get("/api/settings").json()["respond_language"] == "English"
    before = paths.config_file.read_bytes()
    response = client.put("/api/settings/application", json={
        "language": "ru", "respond_language": "Português (Brasil)",
    })
    assert response.status_code == 200
    assert response.json()["respond_language"] == "Português (Brasil)"
    assert response.json()["language"] == "ru"
    assert read_yaml(application_config_file())["respond_language"] == "Português (Brasil)"
    assert load_config(paths).respond_language == "Português (Brasil)"
    assert paths.config_file.read_bytes() == before
    assert client.put("/api/settings/application", json={"language": "en"}).status_code == 200
    assert client.get("/api/settings").json()["respond_language"] == "Português (Brasil)"
    # Project saves must not persist the effective global response preference.
    save_config(paths, client.app.state.ctx.config)
    assert "respond_language" not in read_yaml(paths.config_file)


def test_existing_ui_preference_does_not_set_response_language(project: Path) -> None:
    application_config_file().write_text("language: ru\n", encoding="utf-8")
    config = load_config(ProjectPaths.for_project(project))
    assert config.language == "ru"
    assert config.respond_language == "English"


@pytest.mark.parametrize("patch", [
    {"theme": {"accent": "#123456"}}, {"project_code": "OTHER"},
    {"teams": {"launch_root": "."}}, {"concurrency": {"agents": 7}},
])
def test_global_endpoint_rejects_project_fields(client: TestClient, patch: dict) -> None:
    before = client.get("/api/settings").json()
    assert client.put("/api/settings/application", json=patch).status_code == 422
    assert client.get("/api/settings").json() == before


def test_shared_preferences_update_resident_projects_without_touching_their_data(
    client: TestClient, tmp_path: Path,
) -> None:
    first = client.app.state.ctx
    client.put("/api/settings", json={
        "theme": {"accent": "#112233"}, "project_code": "FIRST",
        "concurrency": {"agents": 3},
    })
    second_root = tmp_path / "another-game"
    second_root.mkdir()
    assert client.post("/api/project/open", json={"path": str(second_root)}).status_code == 200
    second = client.app.state.ctx
    client.put("/api/settings", json={"theme": {"accent": "#445566"}, "project_code": "NEXT"})
    files_before = {ctx.paths.config_file: ctx.paths.config_file.read_bytes() for ctx in (first, second)}
    from voxarium_studio.core.config import default_orchestrator_models
    mapping = default_orchestrator_models()
    mapping["codex"]["1"] = "resident-project-custom-model"

    response = client.put("/api/settings/application", json={
        "theme": {"sidebar": "icons", "mode": "light"}, "language": "ru",
        "respond_language": "日本語",
        "show_data_dir": True, "node_cache_enabled": False, "updates": {"enabled": False},
        "orchestrator_models": mapping,
    })
    assert response.status_code == 200
    for ctx in (first, second):
        assert ctx.config.theme.mode == "light"
        assert ctx.config.theme.sidebar == "icons"
        assert ctx.explorer.config.explorer.show_data_dir is True
        assert ctx.runner.config.node_cache_enabled is False
        assert ctx.config.language == "ru"
        assert ctx.config.respond_language == "日本語"
        assert ctx.config.orchestrator_models == mapping
        assert ctx.updates.config.enabled is False
        assert ctx.paths.config_file.read_bytes() == files_before[ctx.paths.config_file]
        assert load_config(ctx.paths).theme.mode == "light"
    assert first.config.theme.accent == "#112233"
    assert first.config.project_code == "FIRST"
    assert first.jobs.limits["agent"] == 3
    assert second.config.theme.accent == "#445566"
    assert second.config.project_code == "NEXT"
    assert second.jobs.limits["agent"] == 8
    switched = client.post("/api/project/open", json={"path": str(first.paths.root)}).json()
    assert switched["config"]["theme"]["accent"] == "#112233"
    assert switched["config"]["theme"]["mode"] == "light"


async def test_global_save_waits_for_an_in_flight_project_startup(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context not yet registered must still receive the global save and update policy."""
    state = SimpleNamespace(ctx=ctx, project_lock=asyncio.Lock())
    initialise_workspace(state)
    request = Request({"type": "http", "app": SimpleNamespace(state=state)})
    other = tmp_path / "starting-project"
    other.mkdir()
    started = asyncio.Event()
    resume = asyncio.Event()
    save_started = asyncio.Event()
    startup = AppContext.startup

    async def delayed_startup(replacement: AppContext) -> None:
        started.set()  # The replacement already loaded the old application config.
        await resume.wait()
        await startup(replacement)

    monkeypatch.setattr(AppContext, "startup", delayed_startup)
    opening = asyncio.create_task(open_project(state, str(other)))
    saving = None

    async def save() -> dict:
        save_started.set()
        return await update_application_settings(ApplicationSettingsUpdate(
            theme={"mode": "light"}, language="ru", updates={"enabled": False},
        ), request, ctx)

    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        saving = asyncio.create_task(save())
        await asyncio.wait_for(save_started.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not saving.done(), "a global save must wait for the replacement to be registered"
        assert ctx.config.theme.mode == "dark"
        resume.set()
        replacement, saved = await asyncio.wait_for(asyncio.gather(opening, saving), timeout=10)
        assert saved["language"] == "ru"
        assert state.ctx is replacement
        for resident in (ctx, replacement):
            assert resident.config.theme.mode == "light"
            assert resident.config.language == "ru"
            assert resident.config.updates.enabled is False
            assert resident.updates.config.enabled is False
    finally:
        resume.set()
        await asyncio.gather(opening, *([saving] if saving is not None else []), return_exceptions=True)
        for resident in state.project_contexts.values():
            if resident is not ctx:
                await resident.shutdown()


@pytest.mark.parametrize("invalid", [
    {"teams": {"launch_root": "does-not-exist"}},
    {"teams": {"commit_backend": "unknown-backend"}},
    {"project_code": "123"},
])
def test_invalid_mixed_patch_leaves_both_files_and_live_state_unchanged(
    client: TestClient, invalid: dict,
) -> None:
    before = client.get("/api/settings").json()
    ctx = client.app.state.ctx
    limits_before = dict(ctx.jobs.limits)
    files = (ctx.paths.config_file, application_config_file())
    saved = {path: path.read_bytes() for path in files}
    response = client.put("/api/settings", json={
        "theme": {"mode": "light", "accent": "#123456"},
        "concurrency": {"agents": 7}, "language": "ru", **invalid,
    })
    assert response.status_code == 400
    assert client.get("/api/settings").json() == before
    assert dict(ctx.jobs.limits) == limits_before
    assert all(path.read_bytes() == saved[path] for path in files)


def test_legacy_mixed_patch_routes_fields_and_merges_global_preferences(client: TestClient) -> None:
    response = client.put("/api/settings", json={
        "theme": {"mode": "light", "accent": "#123456"}, "project_code": "TASK",
        "language": "ru", "show_data_dir": True,
    })
    assert response.status_code == 200
    client.put("/api/settings/application", json={"theme": {"text_size": None}, "language": None})
    project_data = read_yaml(client.app.state.ctx.paths.config_file)
    global_data = read_yaml(application_config_file())
    assert project_data["theme"] == {"accent": "#123456"}
    assert project_data["project_code"] == "TASK"
    assert "show_data_dir" not in project_data["explorer"]
    assert global_data["theme"]["mode"] == "light"
    assert global_data["theme"]["text_size"] is None
    assert global_data["language"] is None
    assert global_data["show_data_dir"] is True


@pytest.mark.parametrize("text", ["{}", "not: [valid", "theme:\n  text_size: 999\n"])
def test_existing_global_file_is_never_reseeded_from_another_project(
    project: Path, text: str,
) -> None:
    global_file = application_config_file()
    global_file.write_text(text, encoding="utf-8")
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text("theme:\n  mode: light\n  accent: '#123456'\n", encoding="utf-8")
    config = load_config(paths)
    assert config.theme.mode == "dark"
    assert config.theme.accent == "#123456"
    assert global_file.read_text(encoding="utf-8") == text
    assert load_application_config().theme.mode == "dark"


@pytest.mark.parametrize("contents", [b"\x80broken utf8", "theme:\n  mode: light\n".encode("utf-16")])
@pytest.mark.parametrize("target", ["application", "project"])
def test_invalid_config_encoding_preserves_bytes_and_never_seeds_defaults(
    project: Path, contents: bytes, target: str,
) -> None:
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text("theme:\n  mode: light\n  accent: '#123456'\n", encoding="utf-8")
    damaged = application_config_file() if target == "application" else paths.config_file
    damaged.write_bytes(contents)

    config = load_config(paths, migrate_application=True)
    assert config.theme.mode == "dark"
    assert damaged.read_bytes() == contents
    if target == "application":
        assert config.theme.accent == "#123456"
        assert load_application_config().theme.mode == "dark"
    else:
        assert not application_config_file().exists(), "unreadable project data cannot seed user defaults"

# VS-94: storage-only profiles. Lifecycle guards below are fixtures, not runtime wiring.
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from voxarium_studio.core.application_config import (
    ApplicationConfig, RemoteServerProfile, save_application_config,
)
from voxarium_studio.sync import credentials
from voxarium_studio.sync.profiles import ProfileConsumers, ProfileError, RemoteProfiles


@pytest.fixture(autouse=True)
def no_remote_credentials_environment(monkeypatch):
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)


@contextmanager
def unused_profile(_profile_id):
    yield ProfileConsumers()


def test_remote_profiles_metadata_cas_collision_and_preferences_preserved(studio_home):
    repo = RemoteProfiles()
    save_application_config(ApplicationConfig(language="ru"))
    stale = load_application_config()
    first = repo.create("Team", "WSS://REMOTE.example/team/")
    assert first.url == "https://remote.example/team"
    assert first.revision == 1
    with pytest.raises(ProfileError, match="profile_url_collision"):
        repo.create("Other", "remote.example/team")
    renamed = repo.rename(first.profile_id, "New label", expected_revision=1)
    assert renamed.revision == 2
    with pytest.raises(ProfileError, match="profile_revision_conflict"):
        repo.rename(first.profile_id, "stale", expected_revision=1)
    stale.language = "en"
    save_application_config(stale)
    assert repo.get(first.profile_id) == renamed
    assert load_application_config().language == "en"
    assert not credentials.keys_file().exists()
    # HTTP/HTTPS and deployment prefixes are intentionally distinct destinations.
    repo.create("HTTP", "http://remote.example/team")
    repo.create("Prefix", "https://remote.example/other")
    assert len(repo.list()) == 3


def test_remote_profiles_never_seed_from_project_or_enter_effective_config(project):
    repo = RemoteProfiles()
    profile = repo.create("Personal", "https://remote.example")
    paths = ProjectPaths.for_project(project)
    raw = read_yaml(paths.config_file)
    raw["remote_servers"] = [{**profile.model_dump(), "token": "fixture-project-secret"}]
    paths.config_file.write_text(yaml.safe_dump(raw), encoding="utf-8")
    effective = load_config(paths)
    assert "remote_servers" not in effective.model_dump()
    save_config(paths, effective)
    assert "remote_servers" not in read_yaml(paths.config_file)
    assert repo.get(profile.profile_id) == profile


@pytest.mark.parametrize("bad", [
    "remote_servers: [broken]", "remote_servers:\n - profile_id: invalid\n",
    "remote_servers:\n - profile_id: 00000000-0000-0000-0000-000000000001\n   name: Test\n   url: https://host\n   revision: true\n",
    "remote_servers: [unterminated", "not a mapping",
])
def test_remote_profiles_corrupt_registry_never_replaced(studio_home, bad, caplog):
    path = application_config_file()
    path.write_text(bad, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ProfileError, match="application_store_unavailable"):
        RemoteProfiles().create("Any", "https://other.example")
    with pytest.raises(ValueError, match="application_store_unavailable"):
        save_application_config(ApplicationConfig())
    assert path.read_bytes() == before


def test_remote_profiles_corrupt_metadata_errors_and_get_do_not_leak(studio_home, caplog):
    path = application_config_file()
    path.write_text("remote_servers:\n - token: fixture-private-key\n", encoding="utf-8")
    assert load_application_config().remote_servers == []
    assert "fixture-private-key" not in caplog.text
    with pytest.raises(ValidationError) as caught:
        ApplicationConfig.model_validate({"remote_servers": [{"token": "fixture-private-key"}]})
    assert "fixture-private-key" not in str(caught.value)


def test_remote_profiles_application_get_and_all_existing_status_are_secret_free(client):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://remote.invalid")
    profile = repo.set_credential(profile.profile_id, "fixture-private-key", expected_revision=1)
    assert repo.load_token(profile.profile_id) == "fixture-private-key"
    metadata = client.get("/api/settings/application").json()
    assert metadata["remote_servers"] == [profile.model_dump()]
    for url in ("/api/settings/application", "/api/settings", "/api/project", "/api/status", "/api/sync"):
        response = client.get(url)
        assert response.status_code == 200
        assert "fixture-private-key" not in response.text
        assert '"token"' not in response.text
    for payload in ({"remote_servers": []}, {"token": "fixture-injected-key"}):
        assert client.put("/api/settings/application", json=payload).status_code == 422
    # C6 now mounts explicit global metadata creation. It must still be
    # secret-free and must not move profiles into the project's settings.
    created = client.post("/api/remote/servers", json={"name": "Any", "url": "https://elsewhere"})
    assert created.status_code == 201
    assert created.json()["has_token"] is False
    assert "fixture-private-key" not in created.text and '"token"' not in created.text
    assert "fixture-private-key" not in application_config_file().read_text(encoding="utf-8")
    assert "fixture-private-key" not in client.app.state.ctx.paths.config_file.read_text(encoding="utf-8")
    assert "remote_servers" not in client.app.state.ctx.config.model_dump()


def test_remote_profiles_existing_mutations_fail_closed_without_lifecycle(studio_home):
    repo = RemoteProfiles()
    profile = repo.create("Unused?", "https://remote.example")
    before = application_config_file().read_bytes()
    operations = [
        lambda: repo.set_credential(profile.profile_id, "fixture-key", expected_revision=1),
        lambda: repo.clear_credential(profile.profile_id, expected_revision=1),
        lambda: repo.import_legacy_credential(profile.profile_id, expected_revision=1),
        lambda: repo.change_url(profile.profile_id, "https://other.example", expected_revision=1, clear_credential=True),
        lambda: repo.delete(profile.profile_id, expected_revision=1),
    ]
    for operation in operations:
        with pytest.raises(ProfileError, match="consumer_lifecycle_unavailable"):
            operation()
    assert application_config_file().read_bytes() == before
    assert not credentials.keys_file().exists()


def test_remote_profiles_drained_guard_holds_through_credential_publication(studio_home):
    held = False

    @contextmanager
    def guard(_):
        nonlocal held
        held = True
        try:
            yield ProfileConsumers(active_roots=("fixture-root",), drained=True)
        finally:
            held = False

    repo = RemoteProfiles(consumer_guard=guard)
    profile = repo.create("Team", "https://remote.example")
    from voxarium_studio.sync import profiles as module
    original = module._write_application_config
    def checked(config):
        assert held
        original(config)
    # Creation was permitted before a guard; all subsequent writes are inside it.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_write_application_config", checked)
        profile = repo.set_credential(profile.profile_id, "fixture-key", expected_revision=1)
    assert not held and profile.revision == 2
    with pytest.raises(ProfileError, match="profile_in_use"):
        repo.change_url(profile.profile_id, "https://other.example", expected_revision=2, clear_credential=True)


@pytest.mark.parametrize("state", [ProfileConsumers(active_roots=("fixture-root",)), None])
def test_remote_profiles_undrained_or_unknown_consumer_state_rejected(studio_home, state):
    @contextmanager
    def guard(_):
        yield state
    repo = RemoteProfiles(consumer_guard=guard)
    profile = repo.create("Team", "https://remote.example")
    with pytest.raises(ProfileError, match="consumer"):
        repo.set_credential(profile.profile_id, "fixture-key", expected_revision=1)
    assert not credentials.keys_file().exists()


def test_remote_profiles_url_change_revokes_not_transfers_key_and_cas(studio_home):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://first.example/prefix")
    profile = repo.set_credential(profile.profile_id, "fixture-first-key", expected_revision=1)
    repo.create("Other", "https://second.example")
    for url, confirmed, error in [("https://second.example", True, "profile_url_collision"),
                                  ("https://third.example", False, "credential_clear_confirmation_required")]:
        with pytest.raises(ProfileError, match=error):
            repo.change_url(profile.profile_id, url, expected_revision=2, clear_credential=confirmed)
    assert repo.load_token(profile.profile_id) == "fixture-first-key"
    profile = repo.change_url(profile.profile_id, "https://third.example", expected_revision=2, clear_credential=True)
    assert repo.load_token(profile.profile_id) is None
    profile = repo.set_credential(profile.profile_id, "fixture-third-key", expected_revision=3)
    assert repo.load_token(profile.profile_id) == "fixture-third-key"
    assert credentials.load_profile_token(profile.profile_id, "https://first.example/prefix") is None
    profile = repo.clear_credential(profile.profile_id, expected_revision=4)
    assert repo.load_token(profile.profile_id) is None and profile.revision == 5
    repo.delete(profile.profile_id, expected_revision=5)
    with pytest.raises(ProfileError, match="profile_not_found"):
        repo.get(profile.profile_id)


@pytest.mark.parametrize("stage", ["before_metadata", "after_metadata", "url_metadata", "url_after_metadata"])
def test_remote_profiles_cross_file_crash_fails_closed_and_explicit_recovery(studio_home, monkeypatch, stage):
    from voxarium_studio.sync import profiles as module
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://first.example")
    original = module._write_application_config
    def crash(config):
        if stage in ("after_metadata", "url_after_metadata"):
            original(config)
        raise OSError("fixture crash")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_write_application_config", crash)
        with pytest.raises(OSError):
            if stage.startswith("url_"):
                repo.change_url(profile.profile_id, "https://second.example", expected_revision=1, clear_credential=True)
            else:
                repo.set_credential(profile.profile_id, "fixture-uncertain-key", expected_revision=1)
    assert "fixture-uncertain-key" not in repo._journal().read_text(encoding="utf-8")
    with pytest.raises(ProfileError, match="profile_recovery_required"):
        repo.load_token(profile.profile_id)
    with pytest.raises(ProfileError, match="profile_recovery_required"):
        repo.rename(profile.profile_id, "Still blocked", expected_revision=repo.get(profile.profile_id).revision)
    recovered = repo.recover(profile.profile_id, expected_revision=repo.get(profile.profile_id).revision)
    assert recovered and not repo._journal().exists()
    assert repo.load_token(profile.profile_id) is None
    repo.set_credential(profile.profile_id, "fixture-new-key", expected_revision=recovered.revision)
    assert repo.load_token(profile.profile_id) == "fixture-new-key"


def test_remote_profiles_atomic_metadata_failure_keeps_previous_file(studio_home, monkeypatch):
    repo = RemoteProfiles()
    profile = repo.create("Team", "https://remote.example")
    before = application_config_file().read_bytes()
    original = Path.replace
    def fail(path, target):
        if target == application_config_file():
            raise OSError("fixture replace failure")
        return original(path, target)
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        repo.rename(profile.profile_id, "Replacement", expected_revision=1)
    assert application_config_file().read_bytes() == before
    assert not list(studio_home.glob(".config-*.tmp"))


def test_remote_profiles_threads_one_url_and_cas_winner(studio_home):
    repo = RemoteProfiles()
    def create(_):
        try:
            return repo.create("Team", "https://remote.example")
        except ProfileError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(8)))
    profile, = [p for p in results if p is not None]
    def rename(index):
        try:
            return repo.rename(profile.profile_id, str(index), expected_revision=1)
        except ProfileError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(rename, range(8)))
    assert sum(p is not None for p in results) == 1

@pytest.mark.parametrize("token", ["", "   ", "\n\t"])
def test_remote_profiles_invalid_credential_never_starts_transaction(studio_home, token):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://remote.example")
    with pytest.raises(ProfileError, match="credential_required"):
        repo.set_credential(profile.profile_id, token, expected_revision=1)
    assert not repo._journal().exists()
    assert not credentials.keys_file().exists()
    assert repo.rename(profile.profile_id, "Still usable", expected_revision=1).revision == 2


def test_remote_profiles_import_is_explicit_idempotent_and_conflict_keeps_key(studio_home):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    credentials.save_token("https://remote.example", "fixture-legacy-key")
    profile = repo.create("Team", "remote.example")
    assert repo.load_token(profile.profile_id) is None
    profile = repo.import_legacy_credential(profile.profile_id, expected_revision=1)
    assert profile.revision == 2 and repo.load_token(profile.profile_id) == "fixture-legacy-key"
    before = credentials.keys_file().read_bytes()
    profile = repo.import_legacy_credential(profile.profile_id, expected_revision=2)
    assert profile.revision == 2 and credentials.keys_file().read_bytes() == before
    assert credentials.load_token("https://remote.example") == "fixture-legacy-key"
    credentials.save_token("https://remote.example", "fixture-other-legacy-key")
    with pytest.raises(credentials.CredentialConflict):
        repo.import_legacy_credential(profile.profile_id, expected_revision=2)
    assert not repo._journal().exists()
    assert repo.load_token(profile.profile_id) == "fixture-legacy-key"


def test_remote_profiles_duplicate_yaml_keys_are_not_silently_collapsed(studio_home):
    profile = RemoteProfiles().create("Team", "https://remote.example")
    path = application_config_file()
    path.write_text(path.read_text(encoding="utf-8") + "\nremote_servers: []\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ProfileError, match="application_store_unavailable"):
        RemoteProfiles().create("Other", "https://other.example")
    assert path.read_bytes() == before


def test_remote_profiles_fence_durability_order(studio_home, monkeypatch):
    from voxarium_studio.sync import profiles as module
    from voxarium_studio.core import application_config as app_module
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://remote.example")
    events = []
    original_publish = module._write_application_config
    original_keys = credentials.save_profile_token
    def directory(path):
        events.append("directory")
    def publish(config):
        events.append("metadata")
        original_publish(config)
    def secret(*args, **kwargs):
        assert repo._journal().exists() and events == ["directory"]
        events.append("secret")
        return original_keys(*args, **kwargs)
    monkeypatch.setattr(module, "_fsync_directory", directory)
    monkeypatch.setattr(app_module, "_fsync_directory", directory)
    monkeypatch.setattr(module, "_write_application_config", publish)
    monkeypatch.setattr(credentials, "save_profile_token", secret)
    repo.set_credential(profile.profile_id, "fixture-key", expected_revision=1)
    assert events == ["directory", "secret", "metadata", "directory", "directory"]
    assert not repo._journal().exists()


def test_remote_profiles_process_url_collision_has_one_winner(studio_home, tmp_path):
    import os
    import subprocess
    import sys
    code = '''
import sys
from voxarium_studio.sync.profiles import RemoteProfiles, ProfileError
try:
    RemoteProfiles().create('Team', 'https://remote.example')
except ProfileError as exc:
    sys.exit(2 if str(exc) == 'profile_url_collision' else 3)
'''
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    workers = [subprocess.Popen([sys.executable, "-c", code], env=env, cwd=Path(__file__).parents[1],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
    results = []
    try:
        for worker in workers:
            worker.communicate(timeout=30)
            results.append(worker.returncode)
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    assert sorted(results) == [0, 2, 2]
    assert len(RemoteProfiles().list()) == 1


def test_remote_profiles_diagnostic_reads_never_create_files(studio_home):
    repo = RemoteProfiles()
    assert repo.list() == ()
    for method in (repo.get, repo.load_token):
        with pytest.raises(ProfileError, match="profile_not_found"):
            method("00000000-0000-0000-0000-000000000001")
    assert list(studio_home.iterdir()) == []


def test_remote_profiles_delete_crash_recovery_does_not_restore_metadata_or_key(studio_home, monkeypatch):
    from voxarium_studio.sync import profiles as module
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Team", "https://remote.example")
    profile = repo.set_credential(profile.profile_id, "fixture-key", expected_revision=1)
    original = module._write_application_config
    def crash(config):
        original(config)
        raise OSError("fixture crash after deletion")
    with monkeypatch.context() as patch:
        patch.setattr(module, "_write_application_config", crash)
        with pytest.raises(OSError):
            repo.delete(profile.profile_id, expected_revision=2)
    assert repo.list() == () and repo._journal().exists()
    assert repo.recover(profile.profile_id, expected_revision=2) is None
    assert repo.list() == () and not repo._journal().exists()
    assert credentials.load_profile_token(profile.profile_id, profile.url) is None

@pytest.mark.parametrize("conflict", [False, True])
def test_remote_profiles_import_preflight_never_fences_existing_key(studio_home, monkeypatch, conflict):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    credentials.save_token("https://remote.example", "fixture-legacy-key")
    profile = repo.create("Team", "https://remote.example")
    profile = repo.set_credential(profile.profile_id,
                                  "fixture-existing-key" if conflict else "fixture-legacy-key",
                                  expected_revision=1)
    before_keys = credentials.keys_file().read_bytes()
    before_metadata = application_config_file().read_bytes()
    def prohibited(_):
        raise AssertionError("known conflict/no-op must not create a crash fence")
    monkeypatch.setattr(repo, "_begin", prohibited)
    if conflict:
        with pytest.raises(credentials.CredentialConflict):
            repo.import_legacy_credential(profile.profile_id, expected_revision=2)
    else:
        assert repo.import_legacy_credential(profile.profile_id, expected_revision=2) == profile
    assert credentials.keys_file().read_bytes() == before_keys
    assert application_config_file().read_bytes() == before_metadata
    assert not repo._journal().exists()
    assert repo.load_token(profile.profile_id) == ("fixture-existing-key" if conflict else "fixture-legacy-key")


# VS-97 / foundation audit F2: opaque personal data stays on disk, not in DTOs.
@pytest.fixture
def opaque_application_data():
    """Nondefault preferences plus synthetic fields from a future Studio version."""
    from voxarium_studio.core.config import default_orchestrator_models
    return {
        "language": "ru", "respond_language": "Fixture response language",
        "show_data_dir": True, "node_cache_enabled": False,
        "orchestrator_models": default_orchestrator_models(),
        "theme": {
            "mode": "light", "text_size": 17, "font": "inter", "mono_font": "fira",
            "sidebar": "icons", "canvas_grid": "lines", "node_tint": 31,
            "status_palette": "accessible", "custom_tokens": {"--fixture-color": "#123456"},
            "future_theme_flag": 23,
        },
        "updates": {"enabled": False, "check_interval_minutes": 65, "future_update_flag": 29},
        "future_section": {
            "future_flag": 17, "token": "VS97_SYNTHETIC_OPAQUE_SECRET",
            "nested": [None, False, {"label": "future value", "flags": [1, 2]}],
        },
    }


def _seed_opaque_application_data(data):
    """Keep setup registry metadata, replacing only unrelated synthetic preferences."""
    path = application_config_file()
    raw = read_yaml(path) if path.exists() else {}
    raw.update(data)
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _assert_opaque_application_data(data):
    raw = read_yaml(application_config_file())
    assert {key: value for key, value in raw.items() if key != "remote_servers"} == data


@pytest.mark.parametrize("nested", [False, True], ids=["future-section", "future-theme-flag"])
def test_f2_profile_create_preserves_original_audit_fixture(studio_home, nested):
    data = {"language": "ru"}
    if nested:
        data["theme"] = {"mode": "light", "future_theme_flag": 23}
    data["future_section"] = {"future_flag": 17}
    _seed_opaque_application_data(data)
    created = RemoteProfiles().create("Fixture", "https://fixture.invalid/team")
    raw = read_yaml(application_config_file())
    # Defaults may be materialised, but neither future field nor known values may disappear.
    for key, value in data.items():
        if key == "theme":
            assert all(raw[key][field] == expected for field, expected in value.items())
        else:
            assert raw[key] == value
    assert raw["remote_servers"] == [created.model_dump()]


@pytest.mark.parametrize("operation", ["create", "rename", "set", "clear", "url", "delete", "import"])
def test_f2_profile_mutations_preserve_opaque_and_all_preferences(
    studio_home, opaque_application_data, operation,
):
    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Fixture", "https://fixture.invalid/team")
    if operation in ("clear", "url", "delete"):
        profile = repo.set_credential(profile.profile_id, "VS97_SYNTHETIC_PROFILE_KEY", expected_revision=1)
    if operation == "import":
        credentials.save_token(profile.url, "VS97_SYNTHETIC_LEGACY_KEY")
    _seed_opaque_application_data(opaque_application_data)
    # An ordinary settings snapshot is deliberately older than the metadata mutation.
    stale_preferences = load_application_config()
    expected_revision = profile.revision
    if operation == "create":
        repo.create("Second fixture", "https://second.fixture.invalid/prefix")
    elif operation == "rename":
        repo.rename(profile.profile_id, "Renamed fixture", expected_revision=expected_revision)
    elif operation == "set":
        repo.set_credential(profile.profile_id, "VS97_SYNTHETIC_PROFILE_KEY", expected_revision=expected_revision)
    elif operation == "clear":
        repo.clear_credential(profile.profile_id, expected_revision=expected_revision)
    elif operation == "url":
        repo.change_url(profile.profile_id, "https://second.fixture.invalid/prefix",
                        expected_revision=expected_revision, clear_credential=True)
    elif operation == "delete":
        repo.delete(profile.profile_id, expected_revision=expected_revision)
    else:
        repo.import_legacy_credential(profile.profile_id, expected_revision=expected_revision)
    _assert_opaque_application_data(opaque_application_data)
    metadata_after_mutation = read_yaml(application_config_file())["remote_servers"]

    stale_preferences.theme.text_size = 16
    save_application_config(stale_preferences)
    expected = {**opaque_application_data, "theme": {**opaque_application_data["theme"], "text_size": 16}}
    _assert_opaque_application_data(expected)
    assert read_yaml(application_config_file())["remote_servers"] == metadata_after_mutation


@pytest.mark.parametrize("operation", ["set", "url", "delete"])
@pytest.mark.parametrize("published", [False, True], ids=["before-metadata", "after-metadata"])
def test_f2_recovery_preserves_opaque_and_all_preferences(
    studio_home, opaque_application_data, monkeypatch, operation, published,
):
    from voxarium_studio.sync import profiles as module

    repo = RemoteProfiles(consumer_guard=unused_profile)
    profile = repo.create("Fixture", "https://fixture.invalid/team")
    profile = repo.set_credential(profile.profile_id, "VS97_SYNTHETIC_OLD_KEY", expected_revision=1)
    _seed_opaque_application_data(opaque_application_data)
    publish = module._write_application_config

    def crash(config):
        if published:
            publish(config)
        raise OSError("VS97 synthetic publication crash")

    with monkeypatch.context() as patch:
        patch.setattr(module, "_write_application_config", crash)
        with pytest.raises(OSError, match="VS97 synthetic publication crash"):
            if operation == "set":
                repo.set_credential(profile.profile_id, "VS97_SYNTHETIC_UNCERTAIN_KEY", expected_revision=2)
            elif operation == "url":
                repo.change_url(profile.profile_id, "https://second.fixture.invalid/prefix",
                                expected_revision=2, clear_credential=True)
            else:
                repo.delete(profile.profile_id, expected_revision=2)
    _assert_opaque_application_data(opaque_application_data)
    current = next((item for item in repo.list() if item.profile_id == profile.profile_id), None)
    recovered = repo.recover(profile.profile_id, expected_revision=current.revision if current else 2)
    assert (recovered is None) == (operation == "delete" and published)
    assert not repo._journal().exists()
    _assert_opaque_application_data(opaque_application_data)
    if recovered is not None:
        assert repo.load_token(profile.profile_id) is None
    preferences = load_application_config()
    preferences.language = "en"
    save_application_config(preferences)
    _assert_opaque_application_data({**opaque_application_data, "language": "en"})


@pytest.mark.parametrize("bad", [
    b"future_section: {future_flag: 17}\nfuture_section: {token: VS97_SYNTHETIC_OPAQUE_SECRET}\n",
    b"theme: {future_theme_flag: 23, future_theme_flag: 24}\n",
    b"future_section: {nested: {flag: 1, flag: 2}}\n",
    b"defaults: &defaults {language: en}\n<<: *defaults\nlanguage: ru\n",
    b"<<: {language: ru}\n",
    b"future_section: &future {future_flag: 17}\nother_future_section: *future\n",
    b"future_section: &future {recursive: *future}\n",
    b"future_section: !!omap [{future_flag: 17}, {other_flag: 23}]\n",
    b"future_section: !!pairs [{future_flag: 17}, {other_flag: 23}]\n",
    b"future_section: {unclosed: [VS97_SYNTHETIC_OPAQUE_SECRET\n",
    b"future_section: \xff\n",
], ids=["duplicate-top-level", "duplicate-theme", "duplicate-opaque-nested",
        "ambiguous-merge", "literal-merge", "shared-alias", "recursive-alias",
        "ordered-map-tag", "pairs-tag", "corrupt-opaque", "invalid-utf8"])
def test_f2_ambiguous_or_corrupt_opaque_yaml_is_byte_preserved(studio_home, bad, caplog):
    path = application_config_file()
    path.write_bytes(bad)
    with pytest.raises(ProfileError, match="^application_store_unavailable$") as error:
        RemoteProfiles().create("Fixture", "https://fixture.invalid/team")
    assert path.read_bytes() == bad
    with pytest.raises(ValueError, match="^application_store_unavailable$"):
        save_application_config(ApplicationConfig(language="ru"))
    assert path.read_bytes() == bad
    assert not credentials.keys_file().exists()
    assert not RemoteProfiles()._journal().exists()
    assert "VS97_SYNTHETIC_OPAQUE_SECRET" not in str(error.value) + caplog.text


def test_f2_opaque_secret_stays_private_across_get_and_project_save(
    client, opaque_application_data, caplog,
):
    _seed_opaque_application_data(opaque_application_data)
    created = RemoteProfiles().create("Fixture", "https://fixture.invalid/team")
    application = load_application_config()
    assert application.language == "ru"
    assert application.theme.mode == "light"
    assert application.remote_servers == [created]
    assert "future_section" not in application.model_dump()
    assert "future_theme_flag" not in application.theme.model_dump()
    assert "future_update_flag" not in application.updates.model_dump()

    paths = client.app.state.ctx.paths
    effective = load_config(paths)
    assert effective.language == "ru" and effective.theme.mode == "light"
    assert "remote_servers" not in effective.model_dump()
    assert "VS97_SYNTHETIC_OPAQUE_SECRET" not in effective.model_dump_json()
    save_config(paths, effective)
    project_raw = read_yaml(paths.config_file)
    assert "future_section" not in project_raw and "remote_servers" not in project_raw
    assert "VS97_SYNTHETIC_OPAQUE_SECRET" not in paths.config_file.read_text(encoding="utf-8")
    _assert_opaque_application_data(opaque_application_data)

    for url in ("/api/settings/application", "/api/settings", "/api/project", "/api/status", "/api/sync"):
        response = client.get(url)
        assert response.status_code == 200
        assert "VS97_SYNTHETIC_OPAQUE_SECRET" not in response.text
        assert "future_section" not in response.text
        assert "future_theme_flag" not in response.text
        assert "future_update_flag" not in response.text
    response = client.put("/api/settings/application", json={"theme": {"text_size": 16}})
    assert response.status_code == 200
    _assert_opaque_application_data({
        **opaque_application_data, "theme": {**opaque_application_data["theme"], "text_size": 16},
    })
    assert "VS97_SYNTHETIC_OPAQUE_SECRET" not in response.text + caplog.text

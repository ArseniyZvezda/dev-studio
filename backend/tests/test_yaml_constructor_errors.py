"""E1: YAML constructor failures are read errors, not validation errors (VS-106).

Only synthetic config/home files are used. HTTP exercises the production routers
through ASGI without starting an AppContext, lifespan, listener or worker process.
On Windows these async tests must use the task's no-socket test runner: the stock
asyncio loop itself creates sockets, before a test fixture can forbid them.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from pydantic import ValidationError

from voxarium_studio.api.deps import get_ctx
from voxarium_studio.api.project import router, scoped_router
from voxarium_studio.core import application_config, config as config_module
from voxarium_studio.core import project_identity as identity_module
from voxarium_studio.core.config import AppConfig, _read_project_config, load_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project_identity import identity_migration_plan


CANARY = "VS106_SYNTHETIC_CONSTRUCTOR_SECRET_DO_NOT_LOG"
IDENTITY = b"project_identity: {id: good-id, revision: 1}\n"
INVALID_TIMESTAMPS = [
    pytest.param("2026-13-01", id="original-invalid-month"),
    pytest.param("2026-01-32", id="invalid-day"),
    pytest.param("2026-02-29", id="non-leap-day"),
    pytest.param("2026-01-01T24:00:00Z", id="invalid-hour"),
    pytest.param("2026-01-01T00:60:00Z", id="invalid-minute"),
    pytest.param("2026-01-01T00:00:60Z", id="invalid-second"),
    pytest.param("2026-01-01T00:00:00+24:00", id="positive-timezone-limit"),
    pytest.param("2026-01-01T00:00:00-24:00", id="negative-timezone-limit"),
    pytest.param("2026-01-01T00:00:00+23:60", id="timezone-overflow"),
]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, studio_home, monkeypatch):
    home, temporary = tmp_path / "os-home", tmp_path / "os-temp"
    home.mkdir()
    temporary.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def forbidden(*args, **kwargs):
        raise AssertionError("Constructor regressions must not use network/processes")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket.socket, "listen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _snapshot(root: Path) -> dict:
    """Include empty directories, file bytes and nanosecond modification times."""
    return {
        path.relative_to(root).as_posix(): (
            (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
        )
        for path in root.rglob("*")
    }


def _project(tmp_path: Path, studio_home: Path, content: bytes, existing_home=False):
    paths = ProjectPaths.for_project(tmp_path / "managed-project")
    paths.data.mkdir(parents=True)
    paths.config_file.write_bytes(content)
    if existing_home:
        (studio_home / "config.yaml").write_text(
            f"unused: {CANARY}\n", encoding="utf-8",
        )
    return paths


def _timestamp_content(timestamp: str) -> bytes:
    # The bad timestamp is deliberately unused: model field filtering must not
    # hide YAML construction failures or rescue the otherwise valid identity.
    return IDENTITY + f"unused: {timestamp}\nsecret: {CANARY}\n".encode()


def _assert_invalid_plan(plan, paths: ProjectPaths, content: bytes):
    fingerprint = hashlib.sha256(content).hexdigest()
    assert plan.state == "invalid_config"
    assert plan.reasons == ("invalid_config",)
    assert plan.config_fingerprint == fingerprint
    assert plan.migration_plan_id == hashlib.sha256(
        (str(paths.root) + "\0" + fingerprint).encode("utf-8")
    ).hexdigest()
    assert plan.persistent_id is None
    assert plan.identity_revision is None
    assert plan.legacy_url is None
    assert plan.legacy_selector is None
    assert plan.has_legacy_key is None
    assert CANARY not in plan.model_dump_json()


def _assert_yaml_warning(caplog, paths: ProjectPaths, content: bytes):
    records = [r for r in caplog.records if r.name == config_module.__name__]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.msg.startswith("Could not read %s (%s)")
    assert record.msg.endswith("falling back to defaults")
    assert record.args == (paths.config_file, "yaml_error")
    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert str(paths.config_file) in record.getMessage()
    assert "(yaml_error)" in record.getMessage()
    for surface in (
        str(record.msg), repr(record.args), record.getMessage(),
        repr(record.__dict__), caplog.text,
    ):
        assert CANARY not in surface
        assert content.decode("utf-8") not in surface


@pytest.mark.parametrize("existing_home", [False, True])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_constructor_timestamp_plan_is_invalid_without_identity_or_writes(
    tmp_path, studio_home, monkeypatch, caplog, existing_home, timestamp,
):
    content = _timestamp_content(timestamp)
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    read_bytes, read_text = Path.read_bytes, Path.read_text

    def project_bytes(path, *args, **kwargs):
        assert not path.is_relative_to(studio_home), "Plan must not read application home"
        return read_bytes(path, *args, **kwargs)

    def project_text(path, *args, **kwargs):
        assert not path.is_relative_to(studio_home), "Plan must not read application home"
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", project_bytes)
        patch.setattr(Path, "read_text", project_text)
        first = identity_migration_plan(paths)
        assert identity_migration_plan(paths) == first
    _assert_invalid_plan(first, paths, content)
    assert CANARY not in caplog.text
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("existing_home", [False, True])
@pytest.mark.parametrize("entrypoint", ["read", "load", "load_with_migration"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
def test_constructor_timestamp_keeps_read_fallback_and_redacted_yaml_warning(
    tmp_path, studio_home, caplog, existing_home, entrypoint, timestamp,
):
    content = _timestamp_content(timestamp)
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    with caplog.at_level(logging.WARNING, logger=config_module.__name__):
        if entrypoint == "read":
            assert _read_project_config(paths) == ({"project_name": paths.root.name}, False)
        else:
            config = load_config(paths, migrate_application=entrypoint == "load_with_migration")
            assert config.model_dump() == AppConfig(project_name=paths.root.name).model_dump()
            assert config.project_identity is None
    _assert_yaml_warning(caplog, paths, content)
    # In particular, migrate_application=True must not establish a home config
    # from fallback defaults and suppress a later valid first-open migration.
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("existing_home", [False, True])
@pytest.mark.parametrize("route", ["/api/remote/migration", "/api/projects/synthetic/remote/migration"])
@pytest.mark.parametrize("timestamp", INVALID_TIMESTAMPS)
async def test_constructor_timestamp_get_migration_returns_diagnostic_200(
    tmp_path, studio_home, caplog, existing_home, route, timestamp,
):
    content = _timestamp_content(timestamp)
    paths = _project(tmp_path, studio_home, content, existing_home)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(scoped_router, prefix="/api/projects/{project_id}")
    # Keep the real endpoint, parsing, response serialization and exception path;
    # replace only context lookup, avoiding any app startup side effects.
    app.dependency_overrides[get_ctx] = lambda: SimpleNamespace(paths=paths)
    before = _snapshot(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic.invalid",
    ) as http:
        first = await http.get(route)
        second = await http.get(route)
    assert first.status_code == second.status_code == 200
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
    assert first.json() == second.json()
    _assert_invalid_plan(identity_module.IdentityMigrationPlan.model_validate(first.json()), paths, content)
    assert CANARY not in first.text + second.text + caplog.text
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("entrypoint", ["read", "load", "plan"])
def test_constructor_exception_canary_is_not_attached_to_diagnostics(
    tmp_path, studio_home, monkeypatch, caplog, entrypoint,
):
    content = _timestamp_content("2026-01-01")
    paths = _project(tmp_path, studio_home, content)
    before = _snapshot(tmp_path)

    def rejected_constructor(loader, node):
        raise ValueError(CANARY)

    # Patch an actual YAML constructor, not the product parse function. Both
    # SafeLoader and the diagnostic subclass use it, including when the latter
    # has its own constructor table in a future implementation.
    for loader in (yaml.SafeLoader, identity_module._PlanConfigLoader):
        monkeypatch.setattr(loader, "yaml_constructors", {
            **loader.yaml_constructors,
            "tag:yaml.org,2002:timestamp": rejected_constructor,
        })
    with caplog.at_level(logging.WARNING, logger=config_module.__name__):
        if entrypoint == "plan":
            _assert_invalid_plan(identity_migration_plan(paths), paths, content)
        elif entrypoint == "read":
            assert _read_project_config(paths) == ({"project_name": paths.root.name}, False)
        else:
            assert load_config(paths).project_identity is None
    if entrypoint != "plan":
        _assert_yaml_warning(caplog, paths, content)
    for record in caplog.records:
        assert CANARY not in repr(record.__dict__)
        assert record.exc_info is None
    assert CANARY not in caplog.text
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("existing_home", [False, True])
@pytest.mark.parametrize(("timestamp", "expected_type"), [
    pytest.param("2026-01-01", date, id="ordinary-date"),
    pytest.param("2024-02-29", date, id="valid-leap-date"),
    pytest.param("2026-12-31T23:59:59Z", datetime, id="utc-datetime"),
    pytest.param("2026-01-01T00:00:00.123456+03:00", datetime, id="fractional-offset"),
    pytest.param("2026-01-01T00:00:00+23:59", datetime, id="valid-timezone-limit"),
    pytest.param("'2026-13-01'", str, id="quoted-invalid-date-is-string"),
])
def test_valid_timestamps_keep_safe_loader_types_and_stored_identity(
    tmp_path, studio_home, caplog, existing_home, timestamp, expected_type,
):
    content = _timestamp_content(timestamp)
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    raw, successful = _read_project_config(paths)
    assert successful
    assert type(raw["unused"]) is expected_type
    config = load_config(paths)
    assert (config.project_identity.id, config.project_identity.revision) == ("good-id", 1)
    plan = identity_migration_plan(paths)
    assert plan.state == "identity_present"
    assert (plan.persistent_id, plan.identity_revision) == ("good-id", 1)
    assert plan.config_fingerprint == hashlib.sha256(content).hexdigest()
    assert not [r for r in caplog.records if r.name == config_module.__name__]
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("identity", [
    "{id: BAD, revision: 1}",
    "{id: good-id, revision: 0}",
    "{id: good-id, revision: true}",
    f"{{id: good-id, revision: 1, unknown: {CANARY}}}",
])
@pytest.mark.parametrize("existing_home", [False, True])
def test_model_validation_is_not_reclassified_as_yaml_fallback(
    tmp_path, studio_home, caplog, identity, existing_home,
):
    content = f"project_identity: {identity}\nunused: 2024-02-29\n".encode()
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    assert _read_project_config(paths)[1] is True
    with pytest.raises(ValidationError):
        load_config(paths, migrate_application=True)
    _assert_invalid_plan(identity_migration_plan(paths), paths, content)
    assert not [r for r in caplog.records if r.name == config_module.__name__]
    assert CANARY not in caplog.text
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("entrypoint", ["read", "load", "plan"])
def test_value_error_from_disk_read_is_outside_yaml_boundary(
    tmp_path, studio_home, monkeypatch, caplog, entrypoint,
):
    paths = _project(tmp_path, studio_home, IDENTITY)
    before = _snapshot(tmp_path)
    failure = ValueError("synthetic read failure outside parsing")
    method = "read_bytes" if entrypoint == "plan" else "read_text"
    original = getattr(Path, method)

    def rejected_read(path, *args, **kwargs):
        if path == paths.config_file:
            raise failure
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, rejected_read)
        with pytest.raises(ValueError) as caught:
            {"read": _read_project_config, "load": load_config, "plan": identity_migration_plan}[entrypoint](paths)
    assert caught.value is failure
    assert not [r for r in caplog.records if r.name == config_module.__name__]
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("entrypoint", ["load", "plan"])
@pytest.mark.parametrize("stage", ["validation", "scope"])
def test_value_error_after_yaml_parse_propagates(
    tmp_path, studio_home, monkeypatch, caplog, entrypoint, stage,
):
    paths = _project(tmp_path, studio_home, IDENTITY, existing_home=True)
    before = _snapshot(tmp_path)
    failure = ValueError("synthetic failure after YAML parsing")

    def rejected(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        if stage == "validation":
            patch.setattr(AppConfig, "model_validate", rejected)
        else:
            patch.setattr(config_module, "_project_fields", rejected)
        with pytest.raises(ValueError) as caught:
            (load_config if entrypoint == "load" else identity_migration_plan)(paths)
    assert caught.value is failure
    assert not [r for r in caplog.records if r.name == config_module.__name__]
    assert _snapshot(tmp_path) == before


def test_value_error_in_application_overlay_is_not_swallowed(tmp_path, studio_home, monkeypatch):
    paths = _project(tmp_path, studio_home, IDENTITY)
    before = _snapshot(tmp_path)
    failure = ValueError("synthetic application overlay failure")

    def rejected(*args, **kwargs):
        raise failure

    monkeypatch.setattr(application_config, "load_application_config", rejected)
    with pytest.raises(ValueError) as caught:
        load_config(paths)
    assert caught.value is failure
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("content", [
    pytest.param(IDENTITY + b"project_identity: {id: last-id, revision: 2}\n", id="duplicate-identity"),
    pytest.param(IDENTITY + b"unused: {key: first, key: last}\n", id="duplicate-ignored-mapping"),
    pytest.param(b"defaults: &id {id: first-id, revision: 1}\nproject_identity: {<<: *id, id: last-id}\n", id="colliding-merge"),
])
@pytest.mark.parametrize("existing_home", [False, True])
def test_duplicate_policy_remains_distinct_for_runtime_and_diagnostic_loaders(
    tmp_path, studio_home, content, existing_home,
):
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    raw, successful = _read_project_config(paths)
    assert successful
    config = load_config(paths)
    assert config.project_identity.id == raw["project_identity"]["id"]
    assert config.project_identity.id == ("good-id" if b"unused:" in content else "last-id")
    _assert_invalid_plan(identity_migration_plan(paths), paths, content)
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("existing_home", [False, True])
def test_global_scope_filtering_still_follows_application_config_existence(
    tmp_path, studio_home, caplog, existing_home,
):
    content = IDENTITY + b"language: obsolete\nunused: 2024-02-29\n"
    paths = _project(tmp_path, studio_home, content, existing_home)
    before = _snapshot(tmp_path)
    if existing_home:
        assert load_config(paths).project_identity.id == "good-id"
        plan = identity_migration_plan(paths)
        assert (plan.state, plan.persistent_id) == ("identity_present", "good-id")
    else:
        with pytest.raises(ValidationError):
            load_config(paths)
        _assert_invalid_plan(identity_migration_plan(paths), paths, content)
    assert not [r for r in caplog.records if r.name == config_module.__name__]
    assert _snapshot(tmp_path) == before

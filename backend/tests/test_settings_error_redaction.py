"""F1 HTTP regressions: rejected settings/config must not echo their values (§5.2.1).

These exercise the real app, request parsing and JSON exception handlers, not just
``str(ValidationError)``. All credentials below are deliberately synthetic. The
shared conftest isolates Studio/agent homes and disables updates, VCS probes,
Graphify and plugin environment builds; this module also blocks network egress.
"""

from __future__ import annotations

import logging
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from voxarium_studio.api.deps import get_ctx
from voxarium_studio.core.application_config import application_config_file
from voxarium_studio.core.config import ConcurrencyConfig
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import init_project
from voxarium_studio.core.project_scope import canonical_root
from voxarium_studio.main import create_app
from voxarium_studio.sync import service as sync_service


REQUEST_MARKER = "VS96_SYNTHETIC_REJECTED_CREDENTIAL"
STORED_MARKER = "VS96_SYNTHETIC_STORED_CREDENTIAL"
MARKERS = (REQUEST_MARKER, STORED_MARKER)


@pytest.fixture(autouse=True)
def isolated_environment(
    tmp_path: Path,
    studio_home: Path,
    agent_skills_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Mock]:
    """Keep even fallback home/temp lookups local; fail before any outbound socket."""
    home, temporary = tmp_path / "os-home", tmp_path / "os-temp"
    home.mkdir()
    temporary.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, str(home))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(temporary))
    for name in ("VOXARIUM_STUDIO_REMOTE_TOKEN", "VOXARIUM_STUDIO_REMOTE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    blocked = Mock(side_effect=AssertionError("F1 fixtures must not access the network"))
    original_socketpair, original_connect = socket.socketpair, socket.socket.connect
    self_pipe = threading.local()

    def local_socketpair(*args, **kwargs):
        # Windows asyncio uses the stdlib loopback socketpair fallback for its
        # self-pipe. Permit that implementation only, never arbitrary loopback I/O.
        self_pipe.active = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            self_pipe.active = False

    def guarded_connect(sock, address):
        if getattr(self_pipe, "active", False) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return blocked(address)

    monkeypatch.setattr(socket, "socketpair", local_socketpair)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(sync_service, "connect", blocked)
    caplog.set_level(logging.DEBUG)
    yield blocked
    blocked.assert_not_called()
    # Includes all phases, not only the current teardown capture, and tracebacks.
    formatter = logging.Formatter()
    for phase in ("setup", "call", "teardown"):
        for record in caplog.get_records(phase):
            formatted = formatter.format(record)
            for marker in MARKERS:
                assert marker not in formatted


@pytest.fixture
def http(project: Path, isolated_environment: Mock) -> Iterator[TestClient]:
    with TestClient(create_app(str(project)), base_url="http://127.0.0.1") as client:
        yield client


def _open(http: TestClient, root: Path) -> dict:
    response = http.post("/api/projects/open", json={"path": str(root), "create": False})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["project_id"] and result["epoch"]
    assert Path(result["root"]) == root.resolve()
    return result


def _settings_url(http: TestClient, project: Path, kind: str) -> str:
    if kind == "scoped":
        return f"/api/projects/{_open(http, project)['project_id']}/settings"
    return "/api/settings/application" if kind == "application" else "/api/settings"


def _home_bytes(home: Path) -> dict[Path, bytes]:
    return {path.relative_to(home): path.read_bytes() for path in home.rglob("*") if path.is_file()}


def _snapshot(http: TestClient, home: Path, *extra_files: Path) -> dict:
    state = http.app.state
    contexts = tuple(state.project_contexts.values())
    files = {ctx.paths.config_file for ctx in contexts} | set(extra_files)
    return {
        "selected": state.ctx,
        "registry": dict(state.project_registry),
        "roots": dict(state.project_roots),
        "contexts": dict(state.project_contexts),
        "config_objects": tuple(ctx.config for ctx in contexts),
        "configs": tuple(ctx.config.model_dump(mode="json") for ctx in contexts),
        "limits": tuple(dict(ctx.jobs.limits) for ctx in contexts),
        "registries": tuple(ctx.registry for ctx in contexts),
        "forwarding": tuple(ctx.bus.forward_to for ctx in contexts),
        "files": {path: path.read_bytes() if path.exists() else None for path in files},
        "home": _home_bytes(home),
    }


def _assert_unchanged(http: TestClient, home: Path, before: dict) -> None:
    after = _snapshot(http, home, *before["files"])
    assert after == before
    for ctx, original in zip(http.app.state.project_contexts.values(), before["config_objects"]):
        assert ctx.config is original
        assert ctx.config.sync.enabled is False
        assert ctx.sync._runner is None
        assert ctx.sync._ws is None
        assert not ctx.sync.live


def _assert_safe_error(response, status: int, caplog: pytest.LogCaptureFixture) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["detail"]
    for marker in MARKERS:
        assert marker not in response.text
        assert marker not in caplog.text
    return body


def _assert_validation(response, caplog) -> None:
    # Even loc/type/msg may embed rejected input (dynamic dict/extra keys and
    # custom validators). Only this fixed public envelope is safe for settings.
    assert _assert_safe_error(response, 422, caplog) == {"detail": [{
        "type": "value_error", "loc": ["body"], "msg": "Invalid settings request.",
    }]}


@pytest.mark.parametrize(("kind", "payload"), [
    ("legacy", {"token": REQUEST_MARKER}),
    ("legacy", {"sync": {"token": REQUEST_MARKER}}),
    ("application", {"remote_servers": [{"token": REQUEST_MARKER}]}),
    ("scoped", {"token": REQUEST_MARKER}),
], ids=["legacy-token", "legacy-sync-token", "application-profile-list-token", "runtime-token"])
def test_four_audit_put_reproductions_are_redacted_and_atomic(
    http, project, studio_home, caplog, kind, payload,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    response = http.put(url, json=payload)
    _assert_validation(response, caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
@pytest.mark.parametrize("payload", [
    {"token": {"nested": [{"credential": REQUEST_MARKER}]}},
    {"sync": {"enabled": True, "url": "https://fixture.invalid", "token": REQUEST_MARKER}},
    {"project_identity": {"id": "good-id", "revision": 1, "token": REQUEST_MARKER}},
    {"theme": {"accent": {"token": REQUEST_MARKER}}},
    {"theme": [{"token": REQUEST_MARKER}]},
], ids=["nested-extra", "sync-enable-extra", "identity-extra", "nested-type", "list-type"])
def test_nested_and_type_errors_do_not_echo_values_or_apply_valid_siblings(
    http, project, studio_home, caplog, kind, payload,
) -> None:
    url = _settings_url(http, project, kind)
    # Valid siblings would have observable side effects if rejection were partial.
    sibling = {"language": "ru"} if kind == "application" else {"project_name": "must-not-save"}
    before = _snapshot(http, studio_home)
    response = http.put(url, json={**sibling, **payload})
    _assert_validation(response, caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
@pytest.mark.parametrize("payload", [
    {"teams": {"required_documents": [{"token": REQUEST_MARKER}]}},
    {"teams": {"permission_mode": REQUEST_MARKER}},
    {"concurrency": {"cpu": REQUEST_MARKER}},
], ids=["nested-list-element", "validator-context", "number-type"])
def test_list_and_validator_context_errors_are_redacted(
    http, project, studio_home, caplog, kind, payload,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    response = http.put(url, json={"project_name": "must-not-save", **payload})
    _assert_validation(response, caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_non_object_json_request_is_redacted(http, project, studio_home, caplog, kind) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    _assert_validation(http.put(url, json=[{"token": REQUEST_MARKER}]), caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_unknown_field_name_cannot_leak_through_error_location(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    _assert_validation(http.put(url, json={REQUEST_MARKER: "rejected"}), caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application"])
def test_dynamic_dictionary_key_cannot_leak_through_error_location(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    _assert_validation(http.put(url, json={
        "theme": {"custom_tokens": {REQUEST_MARKER: {"invalid": "not-a-string"}}},
    }), caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_malformed_json_uses_the_same_safe_settings_envelope(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    _assert_validation(http.put(url, content='{"token":"' + REQUEST_MARKER + '",',
                                headers={"Content-Type": "application/json"}), caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_multiple_errors_are_not_reflected_individually(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)
    _assert_validation(http.put(url, json={
        REQUEST_MARKER: "extra", "theme": [{"token": REQUEST_MARKER}],
        "project_name": {"token": REQUEST_MARKER}, "language": REQUEST_MARKER,
    }), caplog)
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_custom_dependency_validation_cannot_expose_any_error_member(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    before = _snapshot(http, studio_home)

    def invalid_dependency():
        raise RequestValidationError([{
            "type": REQUEST_MARKER, "loc": ("body", REQUEST_MARKER),
            "msg": f"custom error: {REQUEST_MARKER}", "input": {"token": REQUEST_MARKER},
            "ctx": {"error": ValueError(REQUEST_MARKER)}, "url": REQUEST_MARKER,
        }], body={"token": REQUEST_MARKER})

    http.app.dependency_overrides[get_ctx] = invalid_dependency
    try:
        _assert_validation(http.put(url, json={}), caplog)
    finally:
        http.app.dependency_overrides.pop(get_ctx)
    _assert_unchanged(http, studio_home, before)


INVALID_IDENTITIES = [
    pytest.param({"id": "good-id", "revision": 1, "token": STORED_MARKER}, id="audit-stored-token"),
    pytest.param({"id": "good-id", "revision": 1, "token": [{"nested": STORED_MARKER}]}, id="nested-list"),
    pytest.param({"id": "good-id", "revision": STORED_MARKER}, id="strict-revision-type"),
    pytest.param({"id": STORED_MARKER, "revision": 1}, id="id-validator-context"),
    pytest.param([{"token": STORED_MARKER}], id="identity-list-type"),
]


@pytest.mark.parametrize("endpoint", ["/api/projects/open", "/api/project/open"])
@pytest.mark.parametrize("identity", INVALID_IDENTITIES)
def test_stored_identity_open_errors_are_redacted_without_publishing_a_context(
    http, tmp_path, studio_home, caplog, monkeypatch, endpoint, identity,
) -> None:
    root = tmp_path / "invalid-identity"
    root.mkdir()
    paths = init_project(root, with_samples=False)
    paths.config_file.write_text(yaml.safe_dump({"project_identity": identity}), encoding="utf-8")
    before = _snapshot(http, studio_home, paths.config_file)
    start = AsyncMock(side_effect=AssertionError("Invalid config must not start sync"))
    monkeypatch.setattr(sync_service.RemoteSync, "start", start)
    response = http.post(endpoint, json={"path": str(root), "create": False})
    _assert_safe_error(response, 400, caplog)
    start.assert_not_awaited()
    _assert_unchanged(http, studio_home, before)
    assert (http.get("/api/project")).json()["root"] == str(before["selected"].paths.root)


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
@pytest.mark.parametrize("identity", INVALID_IDENTITIES)
def test_stored_identity_reload_errors_are_redacted_before_any_reconfiguration(
    http, project, tmp_path, studio_home, caplog, monkeypatch, kind, identity,
) -> None:
    if kind == "scoped":
        # A renderer reload must validate its immutable binding, not the selected project.
        root = tmp_path / "non-selected-project"
        root.mkdir()
        init_project(root, with_samples=False)
        opened = _open(http, root)
        ctx = http.app.state.project_registry[opened["project_id"]].ctx
        assert ctx is not http.app.state.ctx
        url = f"/api/projects/{opened['project_id']}/settings/reload"
    else:
        ctx = http.app.state.ctx
        url = "/api/settings/reload"
    ctx.paths.config_file.write_text(yaml.safe_dump({
        "project_name": "must-not-load", "project_identity": identity,
        "sync": {"enabled": True, "url": "https://fixture.invalid", "space": "fixture"},
    }), encoding="utf-8")
    before = _snapshot(http, studio_home)
    reconfigure = AsyncMock(side_effect=AssertionError("Invalid config must not reconfigure sync"))
    plugins = AsyncMock(side_effect=AssertionError("Invalid config must not reload plugins"))
    monkeypatch.setattr(ctx.sync, "reconfigure", reconfigure)
    monkeypatch.setattr(ctx, "reload_plugins", plugins)
    _assert_safe_error(http.post(url), 400, caplog)
    reconfigure.assert_not_awaited()
    plugins.assert_not_awaited()
    _assert_unchanged(http, studio_home, before)


@pytest.mark.parametrize("kind", ["legacy", "application", "scoped"])
def test_valid_settings_remain_usable_after_a_rejected_secret(
    http, project, studio_home, caplog, kind,
) -> None:
    url = _settings_url(http, project, kind)
    _assert_safe_error(http.put(url, json={"token": REQUEST_MARKER}), 422, caplog)
    project_before = ProjectPaths.for_project(project).config_file.read_bytes()
    global_before = application_config_file().read_bytes()
    payload = {"language": "ru", "theme": {"mode": "light"}} if kind == "application" else {
        "project_name": "Accepted fixture", "theme": {"accent": "#123456"}, "docs": {"font_size": 17},
    }
    response = http.put(url, json=payload)
    assert response.status_code == 200, response.text
    effective = http.get("/api/settings").json()
    assert effective["sync"]["enabled"] is False
    if kind == "application":
        assert effective["language"] == "ru" and effective["theme"]["mode"] == "light"
        assert ProjectPaths.for_project(project).config_file.read_bytes() == project_before
    else:
        assert effective["project_name"] == "Accepted fixture"
        assert effective["theme"]["accent"] == "#123456"
        assert effective["docs"]["font_size"] == 17
        assert application_config_file().read_bytes() == global_before


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
def test_valid_open_and_reload_preserve_identity_and_disabled_sync(http, project, kind) -> None:
    opened = _open(http, project)
    assert opened["persistent_id"]
    if kind == "legacy":
        response = http.post("/api/project/open", json={"path": str(project), "create": False})
        assert response.status_code == 200, response.text
    prefix = "/api" if kind == "legacy" else f"/api/projects/{opened['project_id']}"
    response = http.post(prefix + "/settings/reload")
    assert response.status_code == 200, response.text
    assert response.json()["config"]["project_identity"]["id"] == opened["persistent_id"]
    assert response.json()["config"]["sync"]["enabled"] is False
    assert _open(http, project)["project_id"] == opened["project_id"]
    assert http.app.state.ctx.sync._runner is None


@pytest.mark.parametrize("kind", ["legacy", "scoped"])
@pytest.mark.parametrize("stage", ["plugins", "sync"])
@pytest.mark.parametrize("error_kind", ["ordinary", "validation"])
def test_downstream_reload_errors_keep_their_existing_detail(
    http, project, caplog, monkeypatch, kind, stage, error_kind,
) -> None:
    """The config-only redaction catch must not swallow later ValidationErrors."""
    url = _settings_url(http, project, kind) + "/reload"
    ctx = http.app.state.ctx
    if error_kind == "validation":
        with pytest.raises(ValidationError) as caught:
            ConcurrencyConfig.model_validate({"cpu": 0})
        error = caught.value
    else:
        error = ValueError(f"fixture {stage} failure")
    plugins = AsyncMock(return_value=ctx.registry)
    sync = AsyncMock(return_value={})
    boundary = plugins if stage == "plugins" else sync
    boundary.side_effect = error
    monkeypatch.setattr(ctx, "reload_plugins", plugins)
    monkeypatch.setattr(ctx.sync, "reconfigure", sync)
    body = _assert_safe_error(http.post(url), 400, caplog)
    assert body == {"detail": str(error)}
    plugins.assert_awaited_once()
    if stage == "plugins":
        sync.assert_not_awaited()
    else:
        sync.assert_awaited_once()
    assert ctx.config.sync.enabled is False
    assert ctx.sync._runner is None


def test_unrelated_query_validation_keeps_fastapi_diagnostics(http) -> None:
    response = http.get("/api/runs", params={"limit": 0})
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert error["type"] == "greater_than_equal"
    assert error["loc"] == ["query", "limit"]
    assert error["input"] == "0" and error["ctx"] == {"ge": 1}


def test_non_put_settings_validation_keeps_fastapi_diagnostics(http) -> None:
    original_error = {
        "type": "fixture_type", "loc": ["query", "fixture"], "msg": "Fixture diagnostic.",
        "input": "fixture input", "ctx": {"fixture": 1},
    }

    def invalid_dependency():
        raise RequestValidationError([original_error])

    http.app.dependency_overrides[get_ctx] = invalid_dependency
    try:
        response = http.get("/api/settings")
    finally:
        http.app.dependency_overrides.pop(get_ctx)
    assert response.status_code == 422
    assert response.json() == {"detail": [original_error]}


@pytest.mark.parametrize("endpoint", ["/api/projects/open", "/api/project/open"])
def test_unrelated_open_not_found_and_not_initialized_keep_status_and_detail(http, tmp_path, endpoint) -> None:
    missing = tmp_path / "missing-directory"
    response = http.post(endpoint, json={"path": str(missing), "create": False})
    assert response.status_code == 404
    assert response.json()["detail"] == f"No such directory: {canonical_root(missing)}"
    empty = tmp_path / "not-initialized"
    empty.mkdir()
    response = http.post(endpoint, json={"path": str(empty), "create": False})
    assert response.status_code == 409
    assert response.json()["detail"] == f"{canonical_root(empty)} is not a Voxarium Studio project yet."
    assert not ProjectPaths.for_project(empty).data.exists()


def test_unrelated_permission_error_handler_keeps_behavior(http) -> None:
    def permission_failure():
        raise PermissionError("fixture permission denied")

    http.app.dependency_overrides[get_ctx] = permission_failure
    try:
        response = http.get("/api/settings")
    finally:
        http.app.dependency_overrides.pop(get_ctx)
    assert response.status_code == 403
    assert response.json() == {"detail": "fixture permission denied"}

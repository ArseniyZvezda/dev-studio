"""S1 metadata/diagnostics only; every path and home is a fixture (§5.2.1)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from voxarium_studio.core.config import AppConfig, load_config, save_config
from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.core.project import ensure_project, init_project
from voxarium_studio.core.project_identity import ProjectIdentity, identity_migration_plan


@pytest.mark.parametrize("value", ["a", "0", "a._-09", "x" * 64, "d9099d14-d8e8-46d1-aa34-bd4359f770e3"])
def test_identity_accepts_only_canonical_ids(value):
    assert ProjectIdentity(id=value, revision=1).id == value


@pytest.mark.parametrize("value", ["", "A", " a", "a ", "a\n", "-a", ".", "a/b", "..\\a", "a%2fb", "а", "x" * 65, 1, None])
def test_identity_rejects_noncanonical_without_normalising(value):
    with pytest.raises(ValidationError):
        ProjectIdentity(id=value, revision=1)


@pytest.mark.parametrize("revision", [0, -1, True, 1.0, "1", None])
def test_identity_revision_is_strict_positive_integer(revision):
    with pytest.raises(ValidationError):
        ProjectIdentity(id="valid", revision=revision)


def test_identity_forbids_unknown_fields_and_implicit_defaults():
    for payload in ({"id": "ok"}, {"revision": 1}, {"id": "ok", "revision": 1, "space": "other"}):
        with pytest.raises(ValidationError):
            ProjectIdentity.model_validate(payload)
    assert AppConfig().project_identity is None


def _legacy(tmp_path: Path, raw=None, *, content: bytes | None = None, dirname=".voxarium_studio"):
    root = tmp_path / "legacy"
    data = root / dirname
    data.mkdir(parents=True)
    (data / "config.yaml").write_bytes(
        content if content is not None else yaml.safe_dump(raw or {}).encode("utf-8")
    )
    return ProjectPaths.for_inspection(root)


def _snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def test_new_explicit_init_generates_independent_uuid_once(tmp_path, monkeypatch):
    ids = []
    for parent in ("a", "b"):
        root = tmp_path / parent / "same-name"
        root.mkdir(parents=True)
        paths = init_project(root, with_samples=False)
        config = load_config(paths)
        identity = config.project_identity
        assert identity is not None
        assert UUID(identity.id).version == 4
        assert str(UUID(identity.id)) == identity.id
        assert identity.revision == 1
        assert not config.sync.enabled and not config.sync.space
        before = paths.config_file.read_bytes()
        with monkeypatch.context() as patch:
            patch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("new ID on re-init")))
            init_project(root, with_samples=False)
            ensure_project(root)
        assert paths.config_file.read_bytes() == before
        ids.append(identity.id)
    assert ids[0] != ids[1]


@pytest.mark.parametrize("space", ["", " Team-1 ", "broken/selector"])
def test_existing_init_open_and_unrelated_save_preserve_legacy(tmp_path, monkeypatch, space):
    raw = {"project_name": "Original", "sync": {"enabled": False, "url": "https://example.invalid/team", "space": space}}
    paths = _legacy(tmp_path, raw)
    before = paths.config_file.read_bytes()
    monkeypatch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("legacy ID generation")))
    init_project(paths.root, with_samples=False)
    _, config = ensure_project(paths.root)
    assert config.project_identity is None
    assert paths.config_file.read_bytes() == before
    config.project_name = "Renamed"
    save_config(paths, config)
    saved = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
    assert saved["sync"] == raw["sync"]
    assert "project_identity" not in saved


def test_existing_data_without_config_does_not_acquire_identity(tmp_path, monkeypatch):
    root = tmp_path / "partial"
    (root / ".voxarium_studio").mkdir(parents=True)
    monkeypatch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("partial legacy project")))
    assert load_config(init_project(root, with_samples=False)).project_identity is None


@pytest.mark.parametrize("space,state,selector", [
    ("", "needs_identity", None),
    (" Team-1 ", "needs_profile", "team-1"),
    ("broken/selector", "invalid_config", None),
    (" " * 3, "invalid_config", None),
])
def test_plan_is_read_only_and_does_not_resolve_legacy_uuid(tmp_path, monkeypatch, space, state, selector):
    paths = _legacy(tmp_path, {"project_name": "same-name", "sync": {"space": space, **({"url": "wss://EXAMPLE.invalid/prefix/"} if space else {})}})
    before = _snapshot(tmp_path)
    monkeypatch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("diagnostic UUID")))
    # If this code starts consulting credentials/state or sync, the test fails before IO.
    monkeypatch.setattr("voxarium_studio.sync.credentials.load_token", Mock(side_effect=AssertionError("credential IO")))
    monkeypatch.setattr("voxarium_studio.sync.service.RemoteSync.reconfigure", Mock(side_effect=AssertionError("sync IO")))
    plan = identity_migration_plan(paths)
    assert plan.state == state
    assert plan.legacy_selector == selector
    assert plan.persistent_id is None
    assert plan.has_legacy_key is None
    if selector:
        assert plan.legacy_url == "https://example.invalid/prefix"
        assert "legacy_resolution_required" in plan.reasons
    assert identity_migration_plan(paths) == plan
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("persistent,state", [("other", "migration_conflict"), ("team", "needs_profile")])
def test_two_fields_are_not_mapping_evidence(tmp_path, persistent, state):
    paths = _legacy(tmp_path, {"project_identity": {"id": persistent, "revision": 7}, "sync": {"space": " TEAM ", "url": "https://example.invalid"}})
    before = paths.config_file.read_bytes()
    plan = identity_migration_plan(paths)
    assert plan.state == state
    assert plan.persistent_id == persistent and plan.identity_revision == 7
    config = load_config(paths)
    config.project_name = "new label"
    save_config(paths, config)
    raw = yaml.safe_load(paths.config_file.read_bytes())
    assert raw["sync"]["space"] == " TEAM "
    assert raw["project_identity"] == {"id": persistent, "revision": 7}
    assert before != paths.config_file.read_bytes()  # Only the explicit unrelated save wrote.


@pytest.mark.parametrize("content", [b"[not-a-mapping]", b"sync: [", b"\xff", b"", b"project_identity: {id: BAD, revision: 1}", b"sync: {space: 12}"])
def test_malformed_plan_never_repairs_or_exposes_validation_input(tmp_path, content):
    paths = _legacy(tmp_path, content=content)
    before = _snapshot(tmp_path)
    assert identity_migration_plan(paths).state == "invalid_config"
    assert _snapshot(tmp_path) == before


def test_plan_redacts_invalid_url_and_never_reads_home(tmp_path, studio_home):
    paths = _legacy(tmp_path, {"sync": {"space": "team", "url": "https://user:fixture-secret@example.invalid"}})
    before = _snapshot(tmp_path)
    plan = identity_migration_plan(paths)
    assert plan.state == "invalid_config" and plan.legacy_url is None
    assert "fixture-secret" not in plan.model_dump_json()
    assert list(studio_home.iterdir()) == []
    assert _snapshot(tmp_path) == before


def test_missing_plan_config_remains_missing(tmp_path):
    paths = ProjectPaths.for_project(tmp_path)
    before = _snapshot(tmp_path)
    assert identity_migration_plan(paths).state == "invalid_config"
    assert _snapshot(tmp_path) == before


def test_plan_bound_to_root_and_config_bytes_not_labels(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    paths = _legacy(a, {"project_identity": {"id": "chosen-id", "revision": 2}})
    other = _legacy(b, {"project_identity": {"id": "chosen-id", "revision": 2}})
    original = identity_migration_plan(paths)
    assert original.state == "identity_present"
    assert original.config_fingerprint == identity_migration_plan(other).config_fingerprint
    assert original.migration_plan_id != identity_migration_plan(other).migration_plan_id
    paths.config_file.write_bytes(paths.config_file.read_bytes() + b"# manual edit\n")
    changed = identity_migration_plan(paths)
    assert original.config_fingerprint != changed.config_fingerprint
    assert original.migration_plan_id != changed.migration_plan_id
    assert changed.persistent_id == original.persistent_id


def test_plan_f3_ignores_obsolete_text_size_without_reading_home(tmp_path, studio_home, monkeypatch):
    from voxarium_studio.core.application_config import ApplicationConfig, save_application_config

    save_application_config(ApplicationConfig(language="ru"))
    content = b"theme: {text_size: 99}\nproject_identity: {id: good-id, revision: 1}\n"
    paths = _legacy(tmp_path, content=content)
    assert load_config(paths).project_identity.id == "good-id"
    before = _snapshot(tmp_path)
    original_read_bytes, original_read_text = Path.read_bytes, Path.read_text

    def read_bytes(path, *args, **kwargs):
        assert not path.is_relative_to(studio_home), "diagnostic read application/credential bytes"
        return original_read_bytes(path, *args, **kwargs)

    def read_text(path, *args, **kwargs):
        assert not path.is_relative_to(studio_home), "diagnostic read application/credential text"
        return original_read_text(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", read_bytes)
        patch.setattr(Path, "read_text", read_text)
        patch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("diagnostic UUID")))
        patch.setattr("voxarium_studio.sync.credentials.load_token", Mock(side_effect=AssertionError("credential IO")))
        patch.setattr("voxarium_studio.sync.service.RemoteSync.reconfigure", Mock(side_effect=AssertionError("sync IO")))
        plan = identity_migration_plan(paths)
        assert identity_migration_plan(paths) == plan
    assert plan.state == "identity_present"
    assert (plan.persistent_id, plan.identity_revision) == ("good-id", 1)
    assert plan.config_fingerprint == hashlib.sha256(content).hexdigest()
    assert plan.has_legacy_key is None
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("obsolete", [
    {"language": "invalid"},
    {"respond_language": []},
    {"updates": {"check_interval_minutes": -10}},
    {"node_cache_enabled": "invalid"},
    {"explorer": {"show_data_dir": "invalid", "ignore": ["build"]}},
    {"theme": {"mode": "invalid", "accent": "#123456"}},
    {"theme": {"font": "invalid", "node_tint": 999, "accent": "#123456"}},
])
@pytest.mark.parametrize("has_application_config", [False, True])
def test_plan_matches_load_config_global_scope(tmp_path, studio_home, obsolete, has_application_config):
    if has_application_config:
        (studio_home / "config.yaml").write_text("language: ru\n", encoding="utf-8")
    paths = _legacy(tmp_path, {**obsolete, "project_identity": {"id": "good-id", "revision": 1}})
    before = _snapshot(tmp_path)
    if has_application_config:
        assert load_config(paths).project_identity.id == "good-id"
        plan = identity_migration_plan(paths)
        assert plan.state == "identity_present" and plan.persistent_id == "good-id"
    else:
        # Before first-use preference migration these remain legacy effective settings.
        with pytest.raises(ValidationError):
            load_config(paths)
        assert identity_migration_plan(paths).state == "invalid_config"
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("invalid", [
    {"project_identity": {"id": "BAD", "revision": 1}},
    {"project_identity": {"id": "good-id"}},
    {"project_identity": {"id": "good-id", "revision": True}},
    {"project_identity": {"id": "good-id", "revision": 1, "unknown": "private-marker"}},
    {"sync": []},
    {"sync": {"enabled": "invalid"}},
    {"sync": {"space": 12}},
    {"sync": {"url": []}},
    {"sync": {"space": "bad/selector"}},
    {"sync": {"url": "https://user:private-marker@example.invalid"}},
    {"theme": {"text_size": 99, "accent": []}},
    {"theme": []},
    {"theme": None},
    {"explorer": {"show_data_dir": "invalid", "ignore": 12}},
    {"explorer": []},
    {"project_name": []},
    {"concurrency": {"agents": 0}},
    {"docs": {"font_size": 99}},
])
def test_plan_scope_filter_never_repairs_project_owned_fields(tmp_path, studio_home, invalid):
    (studio_home / "config.yaml").write_text("language: ru\n", encoding="utf-8")
    paths = _legacy(tmp_path, {"project_identity": {"id": "good-id", "revision": 1}, **invalid})
    before = _snapshot(tmp_path)
    plan = identity_migration_plan(paths)
    assert plan.state == "invalid_config"
    assert "private-marker" not in plan.model_dump_json()
    assert plan.config_fingerprint == hashlib.sha256(paths.config_file.read_bytes()).hexdigest()
    assert _snapshot(tmp_path) == before


def test_plan_fingerprint_includes_ignored_global_bytes(tmp_path, studio_home):
    (studio_home / "config.yaml").write_text("language: ru\n", encoding="utf-8")
    paths = _legacy(tmp_path, content=b"theme: {text_size: 99}\nproject_identity: {id: good-id, revision: 1}\n")
    original = identity_migration_plan(paths)
    paths.config_file.write_bytes(paths.config_file.read_bytes().replace(b"99", b"98"))
    changed = identity_migration_plan(paths)
    assert original.state == changed.state == "identity_present"
    assert original.persistent_id == changed.persistent_id == "good-id"
    assert original.config_fingerprint != changed.config_fingerprint
    assert original.migration_plan_id != changed.migration_plan_id


@pytest.mark.parametrize("dirname", [".voxarium_studio", "voxarium_studio"])
def test_info_recent_and_config_reads_do_not_generate_or_migrate_identity(tmp_path, monkeypatch, dirname):
    from voxarium_studio import cli
    from voxarium_studio.core.workspace import load_recent, record_recent
    from voxarium_studio.plugins.registry import Registry

    paths = _legacy(tmp_path, {"project_name": "Legacy", "sync": {"space": ""}}, dirname=dirname)
    record_recent(paths.root, "Legacy")
    before = _snapshot(tmp_path)
    monkeypatch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("read-only ID")))
    monkeypatch.setattr(cli, "_build_label", lambda: "fixture-build")
    monkeypatch.setattr(cli, "_describe_vcs", lambda *_: "fixture")
    monkeypatch.setattr("voxarium_studio.plugins.loader.load_all", lambda *_: Registry(paths.root))
    assert load_config(paths).project_identity is None
    cli.info(paths.root)
    assert load_recent()
    assert identity_migration_plan(paths).persistent_id is None
    assert _snapshot(tmp_path) == before


def test_get_plan_reports_disk_problems_without_writes_or_sync(client, project, monkeypatch):
    paths = ProjectPaths.for_project(project)
    paths.config_file.write_text("sync: [", encoding="utf-8")
    before = _snapshot(project)
    monkeypatch.setattr("voxarium_studio.sync.service.RemoteSync.reconfigure", Mock(side_effect=AssertionError("GET reconnect")))
    for _ in range(2):
        response = client.get("/api/remote/migration")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["state"] == "invalid_config"
    assert _snapshot(project) == before


@pytest.mark.parametrize("payload", [
    {"project_identity": {"id": "changed", "revision": 2}},
    {"persistent_id": "changed"},
    {"sync": {"enabled": True, "space": "changed"}},
])
def test_legacy_settings_put_cannot_mutate_identity(client, project, payload):
    before = ProjectPaths.for_project(project).config_file.read_bytes()
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422
    assert ProjectPaths.for_project(project).config_file.read_bytes() == before
    assert client.get("/api/project").json()["persistent_id"] != "changed"


def test_new_mutations_are_not_exposed(client):
    for path in ("/api/remote/prepare", "/api/remote/enable", "/api/remote/migration/apply"):
        assert client.post(path, json={"persistent_id": "changed"}).status_code in (404, 405)


@pytest.mark.parametrize("space,state", [("", "needs_identity"), (" Team ", "needs_profile"), ("a/b", "invalid_config")])
def test_existing_project_metadata_and_both_plan_routes_are_read_only(tmp_path, monkeypatch, space, state):
    from fastapi.testclient import TestClient
    from voxarium_studio.main import create_app

    paths = _legacy(tmp_path, {"sync": {"enabled": False, "space": space, **({"url": "https://example.invalid"} if space else {})}})
    before = paths.config_file.read_bytes()
    monkeypatch.setattr("voxarium_studio.core.project.uuid4", Mock(side_effect=AssertionError("ID on existing open")))
    with TestClient(create_app(str(paths.root)), base_url="http://127.0.0.1") as http:
        opened = http.post("/api/projects/open", json={"path": str(paths.root), "create": False}).json()
        assert opened["persistent_id"] is None
        for prefix in ("/api", f"/api/projects/{opened['project_id']}"):
            metadata = http.get(prefix + "/project")
            assert metadata.status_code == 200
            assert metadata.json()["persistent_id"] is None
            for _ in range(2):
                response = http.get(prefix + "/remote/migration")
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.json()["state"] == state
        assert paths.config_file.read_bytes() == before
    assert paths.config_file.read_bytes() == before


@pytest.fixture(params=[False, True], ids=["home-config-absent", "home-config-present"])
def f4_plan(tmp_path, studio_home, monkeypatch, request):
    """F4 diagnostics may read project bytes and stat, but never load, home config.

    Both home states matter after VS-98's scope filtering. All data is synthetic;
    the byte/mtime snapshot also covers home credentials and private sync state.
    """
    import builtins
    import io
    import os
    import socket

    from voxarium_studio.core import application_config, config, project
    from voxarium_studio.sync import credentials, service

    if request.param:
        (studio_home / "config.yaml").write_bytes(b"language: ru\n")
    (studio_home / "remote-keys.json").write_bytes(b'{"fixture": "F4_PRIVATE_CANARY"}')
    (studio_home / "sync").mkdir()
    (studio_home / "sync" / "fixture.json").write_bytes(b'{"fixture": "F4_PRIVATE_CANARY"}')

    def plan(content):
        paths = _legacy(tmp_path, content=content)
        before = _snapshot(tmp_path)
        guarded_calls = []
        opened = []
        original_open, original_io_open = builtins.open, io.open

        def guard_open(delegate):
            def open_project_only(file, mode="r", *args, **kwargs):
                opened.append((file, mode))
                assert isinstance(file, (str, os.PathLike)), "diagnostic opened a descriptor"
                assert Path(file) == paths.config_file, "diagnostic read outside project config"
                assert mode in ("r", "rb"), "diagnostic wrote a file"
                return delegate(file, mode, *args, **kwargs)
            return open_project_only

        def forbid(patch, owner, name):
            blocked = Mock(side_effect=AssertionError(f"diagnostic called {name}"))
            patch.setattr(owner, name, blocked)
            guarded_calls.append(blocked)

        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", guard_open(original_open))
            patch.setattr(io, "open", guard_open(original_io_open))
            for owner, names in (
                (application_config, ("load_application_config", "save_application_config")),
                (config, ("load_config", "save_config")),
                (project, ("uuid4", "init_project", "ensure_project")),
                (credentials, ("load_token", "load_profile_token", "_read_store", "import_legacy_token")),
                (service.RemoteSync, ("reconfigure",)),
                (socket, ("create_connection", "getaddrinfo")),
                (socket.socket, ("connect", "connect_ex")),
            ):
                for name in names:
                    forbid(patch, owner, name)
            # Catch new UUID call sites and raw descriptor IO too, not just today's imports.
            import uuid
            forbid(patch, uuid, "uuid4")
            forbid(patch, os, "open")
            result = identity_migration_plan(paths)
            assert identity_migration_plan(paths) == result
        for blocked in guarded_calls:
            blocked.assert_not_called()
        assert opened and all(Path(file) == paths.config_file for file, _ in opened)
        assert result.config_fingerprint == hashlib.sha256(content).hexdigest()
        assert result.has_legacy_key is None
        assert "F4_PRIVATE_CANARY" not in result.model_dump_json()
        assert _snapshot(tmp_path) == before
        return result

    return plan


@pytest.mark.parametrize("content", [
    pytest.param(b"project_identity: {id: first, revision: 1}\nproject_identity: {id: second, revision: 2}\n", id="identity-last-wins"),
    pytest.param(b"project_identity: {id: same, revision: 1}\nproject_identity: {id: same, revision: 1}\n", id="identity-identical"),
    pytest.param(b"project_identity: {id: first, id: second, revision: 1}\n", id="id-different"),
    pytest.param(b"project_identity: {id: same, id: same, revision: 1}\n", id="id-identical"),
    pytest.param(b"project_identity: {id: good, revision: 1, revision: 2}\n", id="revision-different"),
    pytest.param(b"project_identity: {id: good, revision: 1, revision: 1}\n", id="revision-identical"),
    pytest.param(b"sync: {space: first}\nsync: {space: second}\n", id="sync-different"),
    pytest.param(b"sync: {space: same}\nsync: {space: same}\n", id="sync-identical"),
    *[
        pytest.param(
            f"sync: {{{key}: {first}, {key}: {second}}}\n".encode(),
            id=f"sync-{key}-{'identical' if first == second else 'different'}",
        )
        for key, first, other in (
            ("space", "first", "second"),
            ("url", "https://first.invalid", "https://second.invalid"),
            ("enabled", "false", "true"),
        )
        for second in (first, other)
    ],
    pytest.param(b"project_identity: &identity {id: good, revision: 1}\nproject_identity: *identity\n", id="duplicate-identity-alias"),
    pytest.param(b"defaults: &identity {id: first, id: second, revision: 1}\nproject_identity: *identity\n", id="aliased-id-duplicate"),
    pytest.param(b"defaults: &identity {id: good, revision: 1, revision: 1}\nproject_identity: *identity\n", id="aliased-revision-identical"),
    pytest.param(b"defaults: &sync {space: first, space: second}\nsync: *sync\n", id="aliased-sync-duplicate"),
    pytest.param(b"defaults: &sync {enabled: false, enabled: false}\nsync: *sync\n", id="aliased-enabled-identical"),
    pytest.param(b"defaults: &sync {url: https://first.invalid, url: https://second.invalid}\nsync: *sync\n", id="aliased-url-duplicate"),
    pytest.param(b"project_identity: {&key id: first, *key: second, revision: 1}\n", id="aliased-mapping-key"),
    pytest.param(b"defaults: &identity {id: first, revision: 1}\nproject_identity: {<<: *identity, id: second}\n", id="merge-explicit-override"),
    pytest.param(b"defaults: &identity {id: good, revision: 1}\nproject_identity: {<<: *identity, id: good}\n", id="merge-identical-override"),
    pytest.param(b"defaults: &identity {id: good, revision: 1}\nproject_identity: {<<: *identity, revision: 2}\n", id="merge-revision-override"),
    pytest.param(b"project_identity: {<<: [{id: first}, {id: second}], revision: 1}\n", id="merge-sequence-collision"),
    pytest.param(b"defaults: &identity {id: good}\nproject_identity: {<<: [*identity, *identity], revision: 1}\n", id="merge-repeated-alias"),
    pytest.param(b"project_identity: {<<: {id: good}, <<: {revision: 1}}\n", id="repeated-merge-key-disjoint-values"),
    pytest.param(b"defaults: &identity {id: good, id: good}\nproject_identity: {<<: *identity, revision: 1}\n", id="merge-source-identical-duplicates"),
    pytest.param(b"defaults: &sync {space: first}\nsync: {<<: *sync, space: second}\n", id="merge-sync-space"),
    pytest.param(b"sync: {<<: [{url: https://first.invalid}, {url: https://second.invalid}]}\n", id="merge-sync-url"),
    pytest.param(b"sync: {<<: [{enabled: false}, {enabled: false}]}\n", id="merge-sync-enabled"),
    pytest.param(b"defaults: &root {project_identity: {id: first, revision: 1}}\n<<: *root\nproject_identity: {id: second, revision: 2}\n", id="merge-root-identity"),
    pytest.param(b"language: en\nlanguage: ru\nproject_identity: {id: good, revision: 1}\n", id="duplicate-global-before-scope-filter"),
    pytest.param(b"unused: {flag: 1, flag: 1}\nproject_identity: {id: good, revision: 1}\n", id="duplicate-opaque-mapping"),
    pytest.param(b"sync: [", id="malformed-yaml"),
    pytest.param(b"", id="empty-document"),
    pytest.param(b"[project_identity, sync]\n", id="nonmapping-document"),
    pytest.param(b"\xff\xfe", id="invalid-utf8"),
    pytest.param(b"---\nproject_identity: {id: good, revision: 1}\n---\nsync: {}\n", id="multiple-documents"),
    pytest.param(b"project_identity: *missing\n", id="undefined-alias"),
    pytest.param(b"project_identity: {<<: 42, id: good, revision: 1}\n", id="merge-scalar"),
    pytest.param(b"project_identity: {<<: [{id: good}, 42], revision: 1}\n", id="merge-invalid-sequence-member"),
    pytest.param(b"? [project_identity]\n: {id: good, revision: 1}\n", id="unhashable-key"),
    pytest.param(b"project_identity: &identity {id: *identity, revision: 1}\n", id="recursive-identity"),
    pytest.param(b"sync: &sync {space: *sync}\n", id="recursive-sync"),
    pytest.param(b"unused: &loop [*loop]\nproject_identity: {id: good, revision: 1}\n", id="recursive-opaque-sequence"),
    pytest.param(b"unused: &loop {self: *loop}\nproject_identity: {id: good, revision: 1}\n", id="recursive-opaque-mapping"),
    pytest.param(b"project_identity: &identity {<<: *identity, id: good, revision: 1}\n", id="recursive-merge"),
    pytest.param(b"unused: &loop {<<: [*loop]}\nproject_identity: {id: good, revision: 1}\n", id="recursive-merge-sequence"),
])
def test_plan_f4_rejects_ambiguous_or_unsafe_yaml(f4_plan, content):
    # Deliberately conservative: even legal explicit merge overrides are ambiguous
    # diagnostic input. Never select a winner, including in ignored global fields.
    plan = f4_plan(content)
    assert plan.state == "invalid_config"
    assert (plan.persistent_id, plan.identity_revision) == (None, None)
    assert (plan.legacy_selector, plan.legacy_url) == (None, None)


@pytest.mark.parametrize("content", [
    pytest.param(b"project_identity: {id: good._-09, revision: 7}\n", id="plain-canonical-id"),
    pytest.param(b"defaults: &identity {id: good._-09, revision: 7}\nproject_identity: *identity\n", id="mapping-alias"),
    pytest.param(b"project_name: &id good._-09\nproject_identity: {id: *id, revision: 7}\n", id="scalar-alias"),
    pytest.param(b"defaults: &identity {id: good._-09}\nproject_identity: {<<: *identity, revision: 7}\n", id="noncolliding-merge"),
    pytest.param(b"project_identity: {<<: [{id: good._-09}, {revision: 7}]}\n", id="noncolliding-merge-sequence"),
    pytest.param(b"defaults: &identity {id: good._-09}\ncombined: &combined {<<: *identity, revision: 7}\nproject_identity: {<<: *combined}\n", id="nested-noncolliding-merge"),
    pytest.param(b"defaults: &identity {id: good._-09, revision: 7}\nfirst: *identity\nsecond: *identity\nproject_identity: *identity\n", id="shared-alias-not-cycle"),
    pytest.param(b"project_identity: {id: good._-09, revision: 7}\nsync: {<<: [{enabled: false}, {space: ''}, {url: ''}]}\n", id="noncolliding-sync-merge"),
])
def test_plan_f4_accepts_unambiguous_aliases_and_merges(f4_plan, content):
    plan = f4_plan(content)
    assert plan.state == "identity_present"
    assert (plan.persistent_id, plan.identity_revision) == ("good._-09", 7)
    assert (plan.legacy_selector, plan.legacy_url) == (None, None)


@pytest.mark.parametrize("content", [
    pytest.param(b"defaults: &sync {enabled: false, space: ' TEAM ', url: 'wss://EXAMPLE.invalid/prefix/'}\nsync: *sync\n", id="legacy-sync-alias"),
    pytest.param(b"sync: {<<: [{enabled: false}, {space: ' TEAM '}, {url: 'wss://EXAMPLE.invalid/prefix/'}]}\n", id="legacy-sync-merge"),
])
def test_plan_f4_unambiguous_legacy_aliases_keep_selector_semantics(f4_plan, content):
    plan = f4_plan(content)
    assert plan.state == "needs_profile"
    assert (plan.persistent_id, plan.identity_revision) == (None, None)
    assert (plan.legacy_selector, plan.legacy_url) == ("team", "https://example.invalid/prefix")


@pytest.mark.parametrize("value", ["Good", " good", "good ", "good/child", "а", "x" * 65])
@pytest.mark.parametrize("aliased", [False, True], ids=["plain", "alias"])
def test_plan_f4_does_not_normalise_persistent_ids(f4_plan, value, aliased):
    scalar = yaml.safe_dump(value, default_style='"').strip()
    content = (
        f"project_name: &id {scalar}\nproject_identity: {{id: *id, revision: 1}}\n"
        if aliased else f"project_identity: {{id: {scalar}, revision: 1}}\n"
    )
    plan = f4_plan(content.encode("utf-8"))
    assert plan.state == "invalid_config"
    assert (plan.persistent_id, plan.identity_revision) == (None, None)
    assert (plan.legacy_selector, plan.legacy_url) == (None, None)

# I6 guarded project publication (runtime-integration-plan §3.2–3.5).
def _i6_commit(paths, *, persistent_id="new-local", revision=2, url="https://new.invalid/team",
               fingerprint=None):
    from voxarium_studio.core.config import commit_disabled_identity, read_project_config_snapshot

    if fingerprint is None:
        fingerprint = read_project_config_snapshot(paths).fingerprint
    return commit_disabled_identity(paths, identity=ProjectIdentity(id=persistent_id, revision=revision),
                                    url=url, expected_fingerprint=fingerprint)


def test_i6_disabled_commit_is_canonical_and_preserves_unrelated_bytes_semantics(tmp_path):
    paths = _legacy(tmp_path, {
        "project_identity": {"id": "old-local", "revision": 1},
        "sync": {"enabled": True, "url": "https://old.invalid", "space": "legacy"},
        "project_name": "Unchanged", "future_setting": {"opaque": [1, 2]},
    })
    docs = paths.data / "docs"
    docs.mkdir()
    (docs / "note.md").write_bytes(b"# Source\r\n")
    before_docs = _snapshot(docs)
    committed = _i6_commit(paths)
    raw = yaml.safe_load(paths.config_file.read_bytes())
    assert raw["project_identity"] == {"id": "new-local", "revision": 2}
    assert raw["sync"] == {"enabled": False, "url": "https://new.invalid/team"}
    assert raw["project_name"] == "Unchanged" and raw["future_setting"] == {"opaque": [1, 2]}
    assert committed.fingerprint == hashlib.sha256(paths.config_file.read_bytes()).hexdigest()
    assert committed.config.project_identity == ProjectIdentity(id="new-local", revision=2)
    assert not committed.config.sync.enabled and committed.config.sync.space == ""
    assert _snapshot(docs) == before_docs


@pytest.mark.parametrize("identity", [None, {"id": "old-local", "revision": 1}])
def test_i6_stale_settings_cannot_undo_identity_target_or_resurrect_space(tmp_path, identity):
    raw = {"sync": {"enabled": True, "url": "https://old.invalid", "space": "legacy"}}
    if identity is not None:
        raw["project_identity"] = identity
    paths = _legacy(tmp_path, raw)
    stale = load_config(paths)
    committed = _i6_commit(paths, revision=2 if identity else 1)
    stale.project_name = "New label from old settings view"
    stale.theme.accent = "#123456"
    save_config(paths, stale)
    after = yaml.safe_load(paths.config_file.read_bytes())
    assert after["project_identity"] == committed.config.project_identity.model_dump()
    assert after["sync"] == {"enabled": False, "url": "https://new.invalid/team"}
    assert after["project_name"] == stale.project_name
    assert after["theme"]["accent"] == "#123456"
    # Settings APIs copy the supplied draft back into the same resident config.
    assert stale.project_identity == committed.config.project_identity
    assert stale.sync == committed.config.sync


@pytest.mark.parametrize("revision", [1, 3, 4])
def test_i6_commit_requires_exact_next_revision(tmp_path, revision):
    from voxarium_studio.core.config import ProjectConfigError

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    before = paths.config_file.read_bytes()
    with pytest.raises(ProjectConfigError, match="config_revision_conflict"):
        _i6_commit(paths, revision=revision)
    assert paths.config_file.read_bytes() == before


@pytest.mark.parametrize("fingerprint", ["", "*", True, 1, "g" * 64, "A" * 64])
def test_i6_commit_rejects_invalid_fingerprint(tmp_path, fingerprint):
    from voxarium_studio.core.config import ProjectConfigError

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    before = paths.config_file.read_bytes()
    with pytest.raises(ProjectConfigError, match="invalid_identity_commit"):
        _i6_commit(paths, fingerprint=fingerprint)
    assert paths.config_file.read_bytes() == before


@pytest.mark.parametrize("url", ["https://USER:private-fixture-canary@example.invalid", "HTTPS://example.invalid/", 1, None])
def test_i6_commit_rejects_noncanonical_or_unsafe_url(tmp_path, url):
    from voxarium_studio.core.config import ProjectConfigError

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    before = paths.config_file.read_bytes()
    with pytest.raises(ProjectConfigError, match="invalid_identity_commit") as caught:
        _i6_commit(paths, url=url)
    assert "private-fixture-canary" not in str(caught.value)
    assert paths.config_file.read_bytes() == before


def test_i6_commit_checks_exact_bytes_not_only_revision(tmp_path):
    from voxarium_studio.core.config import ProjectConfigError, read_project_config_snapshot

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    observed = read_project_config_snapshot(paths)
    before = paths.config_file.read_bytes() + b"# VCS edit without revision change\n"
    paths.config_file.write_bytes(before)
    with pytest.raises(ProjectConfigError, match="config_revision_conflict"):
        _i6_commit(paths, fingerprint=observed.fingerprint)
    assert paths.config_file.read_bytes() == before


def test_i6_config_cas_rechecked_after_staging_before_replace(tmp_path, monkeypatch):
    from voxarium_studio.core import config as module
    from voxarium_studio.core.config import ProjectConfigError

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    external = paths.config_file.read_bytes() + b"# concurrent outside writer\n"
    original_fsync = module.os.fsync

    def fsync(fd):
        original_fsync(fd)
        paths.config_file.write_bytes(external)
    monkeypatch.setattr(module.os, "fsync", fsync)
    with pytest.raises(ProjectConfigError, match="config_revision_conflict"):
        _i6_commit(paths)
    assert paths.config_file.read_bytes() == external
    assert not list(paths.data.glob("*.tmp"))


@pytest.mark.parametrize("content", [b"", b"sync: [", b"\xff", b"[not-a-map]", b"project_identity: {id: BAD, revision: 1}",
                                     b"sync: {url: x, url: y}", b"theme: [not-a-map]", b"project_identity: &x {id: old-local, revision: 1}\nsync: *x\n"])
def test_i6_malformed_commit_and_settings_never_default_overwrite(tmp_path, content):
    from voxarium_studio.core.config import ProjectConfigError, commit_disabled_identity

    paths = _legacy(tmp_path, content=content)
    before = _snapshot(paths.root)
    with pytest.raises(ProjectConfigError, match="invalid_config"):
        commit_disabled_identity(paths, identity=ProjectIdentity(id="new-local", revision=1), url="",
                                 expected_fingerprint=hashlib.sha256(content).hexdigest())
    with pytest.raises(ProjectConfigError, match="invalid_config"):
        save_config(paths, AppConfig(project_name="Cannot repair"))
    assert _snapshot(paths.root) == before


@pytest.mark.parametrize("boundary", ["tempfile", "write", "fsync", "replace"])
def test_i6_config_publication_fault_never_changes_old_identity(tmp_path, monkeypatch, boundary):
    from contextlib import contextmanager
    from voxarium_studio.core import config as module
    from voxarium_studio.core.config import ProjectConfigError

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    before = paths.config_file.read_bytes()
    original_tempfile = module.tempfile.NamedTemporaryFile

    def fail(*_args, **_kwargs):
        raise OSError("private-fixture-canary")
    with monkeypatch.context() as patch:
        if boundary == "tempfile":
            patch.setattr(module.tempfile, "NamedTemporaryFile", fail)
        elif boundary == "write":
            @contextmanager
            def short_file(*args, **kwargs):
                with original_tempfile(*args, **kwargs) as stream:
                    class Stream:
                        def __getattr__(self, name):
                            return getattr(stream, name)

                        def write(self, content):
                            return stream.write(content[:len(content) // 2])
                    yield Stream()
            patch.setattr(module.tempfile, "NamedTemporaryFile", short_file)
        elif boundary == "fsync":
            patch.setattr(module.os, "fsync", fail)
        else:
            patch.setattr(Path, "replace", fail)
        with pytest.raises(ProjectConfigError, match="config_store_unavailable") as caught:
            _i6_commit(paths)
        assert caught.value.__context__ is None
    assert paths.config_file.read_bytes() == before
    assert not list(paths.data.glob("*.tmp"))


def test_i6_config_internal_seam_does_not_acquire_another_os_lock(tmp_path, monkeypatch):
    from voxarium_studio.core import application_config
    from voxarium_studio.core.config import _commit_disabled_identity_in_transaction, read_project_config_snapshot

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    observed = read_project_config_snapshot(paths)
    transaction = application_config.application_config_transaction
    with transaction():
        monkeypatch.setattr(application_config, "application_config_transaction",
                            Mock(side_effect=AssertionError("nested OS lock")))
        committed = _commit_disabled_identity_in_transaction(
            paths, identity=ProjectIdentity(id="new-local", revision=2), url="",
            expected_fingerprint=observed.fingerprint,
        )
    assert committed.config.sync.url == "" and not committed.config.sync.enabled


def test_i6_read_snapshot_missing_is_read_only_and_strictly_project_only(tmp_path, studio_home, monkeypatch):
    from voxarium_studio.core.config import read_project_config_snapshot

    paths = ProjectPaths.for_project(tmp_path / "not-created")
    before = _snapshot(tmp_path)
    monkeypatch.setattr("voxarium_studio.core.application_config.load_application_config",
                        Mock(side_effect=AssertionError("home read")))
    observed = read_project_config_snapshot(paths)
    assert observed.fingerprint is None and observed.config.project_identity is None
    assert _snapshot(tmp_path) == before and list(studio_home.iterdir()) == []


def test_i6_commit_and_private_publication_share_one_real_os_lock(tmp_path, monkeypatch):
    import os
    from voxarium_studio.core import application_config
    from voxarium_studio.core.application_config import RemoteServerProfile
    from voxarium_studio.core.config import _commit_disabled_identity_in_transaction, read_project_config_snapshot
    from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    observed = read_project_config_snapshot(paths)
    calls = []
    if os.name == "nt":
        import msvcrt
        locking = msvcrt.locking

        def counted(fd, mode, count):
            if mode == msvcrt.LK_LOCK:
                calls.append(mode)
            return locking(fd, mode, count)
        monkeypatch.setattr(msvcrt, "locking", counted)
    else:
        import fcntl
        flock = fcntl.flock

        def counted(fd, mode):
            if mode == fcntl.LOCK_EX:
                calls.append(mode)
            return flock(fd, mode)
        monkeypatch.setattr(fcntl, "flock", counted)
    store = RemoteBindings()
    with application_config.application_config_transaction():
        committed = _commit_disabled_identity_in_transaction(
            paths, identity=ProjectIdentity(id="new-local", revision=2), url="https://new.invalid",
            expected_fingerprint=observed.fingerprint,
        )
        profile = RemoteServerProfile(profile_id="c503da2b-f8fe-4fb0-b945-86bc119495b3",
                                      name="Synthetic", url="https://new.invalid")
        private = store._save_in_transaction(BindingRecord.for_root(
            paths.root, profile=profile, identity=committed.config.project_identity, selection_state="selected",
        ), expected_fingerprint=None)
        removed = store._remove_in_transaction(paths.root, expected_fingerprint=private.fingerprint)
    assert len(calls) == 1 and not removed.records


def test_i6_settings_and_commit_race_preserves_latest_identity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from voxarium_studio.core.config import ProjectConfigError, read_project_config_snapshot

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1},
                               "sync": {"url": "https://old.invalid", "space": "legacy", "enabled": False}})
    before = read_project_config_snapshot(paths)
    stale = load_config(paths)
    stale.project_name = "Concurrent label"
    barrier = Barrier(2)

    def settings():
        barrier.wait(timeout=10)
        save_config(paths, stale)
        return "saved"

    def commit():
        barrier.wait(timeout=10)
        try:
            return _i6_commit(paths, fingerprint=before.fingerprint)
        except ProjectConfigError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(settings), pool.submit(commit)
        assert a.result(timeout=10) == "saved"
        outcome = b.result(timeout=10)
    raw = yaml.safe_load(paths.config_file.read_bytes())
    assert raw["project_name"] == "Concurrent label"
    if outcome == "config_revision_conflict":
        assert raw["project_identity"] == {"id": "old-local", "revision": 1}
    else:
        assert raw["project_identity"] == {"id": "new-local", "revision": 2}
        assert raw["sync"] == {"url": "https://new.invalid/team", "enabled": False}


def test_i6_settings_http_keeps_committed_target_in_disk_and_resident_config(client):
    ctx = client.app.state.ctx
    old = ctx.config.project_identity
    assert old is not None
    committed = _i6_commit(ctx.paths, revision=old.revision + 1)
    # Deliberately retain the resident's pre-cutover snapshot, like an in-flight
    # settings draft. Its eventual API publication must not undo the commit.
    response = client.put("/api/settings", json={"project_name": "Late settings response"})
    assert response.status_code == 200, response.text
    current = load_config(ctx.paths)
    assert current.project_identity == committed.config.project_identity
    assert ctx.config.project_identity == committed.config.project_identity
    assert current.sync == committed.config.sync == ctx.config.sync
    assert "space" not in yaml.safe_load(ctx.paths.config_file.read_bytes())["sync"]


@pytest.mark.parametrize("enabled", [False, True])
def test_i6_unrelated_settings_keep_current_canonical_enabled_state(tmp_path, enabled):
    paths = _legacy(tmp_path, {"project_identity": {"id": "already-active", "revision": 9},
                               "sync": {"enabled": enabled, "url": "https://current.invalid"}})
    stale = AppConfig(project_identity=ProjectIdentity(id="stale", revision=1))
    stale.sync.enabled = not enabled
    stale.sync.space = "never-resurrect"
    stale.sync.url = "https://wrong.invalid"
    save_config(paths, stale)
    raw = yaml.safe_load(paths.config_file.read_bytes())
    assert raw["sync"] == {"enabled": enabled, "url": "https://current.invalid"}
    assert raw["project_identity"] == {"id": "already-active", "revision": 9}
    assert stale.project_identity.revision == 9 and stale.sync.enabled is enabled


def test_i6_settings_cannot_seed_identity_into_existing_legacy(tmp_path):
    paths = _legacy(tmp_path, {"sync": {"space": "old-space", "url": "https://old.invalid", "enabled": False}})
    stale = load_config(paths)
    stale.project_identity = ProjectIdentity(id="unconsented", revision=1)
    save_config(paths, stale)
    assert "project_identity" not in yaml.safe_load(paths.config_file.read_bytes())
    assert stale.project_identity is None


@pytest.mark.parametrize("kind", ["directory", "symlink", "hardlink"])
def test_i6_config_locator_must_be_regular_unlinked_file(tmp_path, monkeypatch, kind):
    from types import SimpleNamespace
    import stat
    from voxarium_studio.core.config import ProjectConfigError, read_project_config_snapshot

    paths = _legacy(tmp_path, {"project_identity": {"id": "old-local", "revision": 1}})
    before = paths.config_file.read_bytes()
    lstat = Path.lstat

    def changed_locator(path):
        if path == paths.config_file:
            return SimpleNamespace(st_mode=stat.S_IFDIR if kind == "directory" else
                                   stat.S_IFLNK if kind == "symlink" else stat.S_IFREG,
                                   st_nlink=2 if kind == "hardlink" else 1)
        return lstat(path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", changed_locator)
        with pytest.raises(ProjectConfigError, match="config_store_unavailable"):
            read_project_config_snapshot(paths)
        with pytest.raises(ProjectConfigError, match="config_store_unavailable"):
            save_config(paths, AppConfig())
    assert paths.config_file.read_bytes() == before

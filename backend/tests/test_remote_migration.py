"""I15: explicit migration of ONE explicitly selected legacy root.

remote-identity/runtime-integration-plan §4 I15; studio-design §5.2/§9.
The Remote is real and LEGACY: a v1 SQLite database (spaces.id = old UUID,
spaces.name = the old selector) upgraded by the Remote's own offline
``migrate_database`` (catalog ID = storage_epoch = old UUID, exact
legacy_spaces mapping). The Studio app, its catalog owner, the production
CatalogHttpTransport and the profile/credential owners are real; ASGI
replaces only the network hop. Every sync WebSocket connect is recorded and
refused: no migration step may open one. A missing Remote is a failed
prerequisite, never a skip.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from test_remote_catalog_activation import KEY_A, KEY_B, secret_free
from voxarium_studio import main
from voxarium_studio.core.paths import ProjectPaths, user_config_dir
from voxarium_studio.sync import catalog_runtime, credentials
from voxarium_studio.sync import service as sync_service
from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.state import Base, SyncState, SyncStateV2

URL = "http://127.0.0.1:8787"
RAW_URL = URL + "/"  # As once typed into the legacy settings; normalised by the plan.
RAW_NAME = "Boloto"  # As typed; the old check_space_name rule lowercases it.
NAME = "boloto"  # The effective legacy selector, i.e. the v1 space name.
PROFILE = "c503da2b-f8fe-4fb0-b945-86bc11949515"
DOCS = (("a.md", "# A\n"), ("notes/b.md", "# B\n"))


def legacy_remote(path: Path, *, space_id: str, name: str = NAME, docs=DOCS) -> None:
    """A v1 Remote database, then the Remote's explicit offline v1 -> v2 upgrade."""
    from voxarium_studio_remote.auth import hash_token
    from voxarium_studio_remote.migrations import V1_SCHEMA, migrate_database
    from voxarium_studio_remote.protocol import content_hash

    db = sqlite3.connect(path)
    db.executescript(V1_SCHEMA)
    user = uuid.uuid4().hex
    stamp = "2026-01-01T00:00:00Z"
    db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)",
               (user, "alice", "#3366cc", hash_token(KEY_B), stamp, None))
    db.execute("INSERT INTO spaces VALUES (?,?,?,?)", (space_id, name, len(docs), stamp))
    for rev, (doc, content) in enumerate(docs, 1):
        db.execute("INSERT INTO docs VALUES (?,?,?,?,?,0,?,?)",
                   (space_id, doc, content, content_hash(content), rev, user, stamp))
    db.commit()
    db.close()
    report = migrate_database(path, backup_path=path.with_suffix(".backup"), confirm_offline=True)
    (project,) = report["projects"]
    assert (project["id"], project["storage_epoch"], project["legacy_space_name"]) == (
        space_id, space_id, name)


def seed_home(home: Path, *, profile=True, stored=KEY_B, legacy=KEY_B) -> None:
    home.mkdir(parents=True, exist_ok=True)
    rows = [{"profile_id": PROFILE, "name": "Team", "url": URL, "revision": 1}] if profile else []
    (home / "config.yaml").write_text(json.dumps({"remote_servers": rows}), encoding="utf-8")
    keys = {"version": 2, "profiles": {}, "legacy": {URL: legacy} if legacy else {}}
    if profile and stored:
        keys["profiles"][PROFILE] = {"url": URL, "token": stored, "revision": 1}
    (home / "remote-keys.json").write_text(json.dumps(keys), encoding="utf-8")


def legacy_config(project: Path, *, space=RAW_NAME, enabled=False, identity=None) -> None:
    config = ProjectPaths.for_project(project).config_file
    raw = yaml.safe_load(config.read_text(encoding="utf-8"))
    raw.pop("project_identity", None)
    if identity is not None:
        raw["project_identity"] = identity
    raw["sync"] = {"enabled": enabled, "url": RAW_URL, "space": space}
    config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


@asynccontextmanager
async def migration_studio(project, monkeypatch, home, *, profile=True, stored=KEY_B,
                           legacy=KEY_B, enabled=False, identity=None, space=RAW_NAME):
    from voxarium_studio.core.context import AppContext
    from voxarium_studio.explorer.watcher import FileWatcher
    from voxarium_studio_remote.app import create_app as create_remote_app
    from voxarium_studio_remote.store import Store

    monkeypatch.setattr(FileWatcher, "start", lambda self: None)
    monkeypatch.setattr(AppContext, "_start_plugin_hooks", lambda self: None)
    monkeypatch.delenv(credentials.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(credentials.URL_ENV_VAR, raising=False)
    connects: list[str] = []

    def no_socket(address, *args, **kwargs):
        connects.append(address)
        raise OSError("no sync socket in this test")

    monkeypatch.setattr(sync_service, "connect", no_socket)
    space_id = uuid.uuid4().hex
    database = home.parent / "remote" / "remote.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    legacy_remote(database, space_id=space_id)
    store = Store(database)
    remote = create_remote_app(store=store)
    calls: list[tuple[str, str]] = []

    @remote.middleware("http")
    async def observe(request, call_next):
        calls.append((request.method, request.url.path))
        return await call_next(request)

    monkeypatch.setattr(catalog_runtime, "CatalogHttpTransport",
                        lambda: CatalogHttpTransport(transport=httpx.ASGITransport(app=remote)))
    seed_home(home, profile=profile, stored=stored, legacy=legacy)
    legacy_config(project, space=space, enabled=enabled, identity=identity)
    s = type("Studio", (), {})()
    s.store, s.calls, s.connects = store, calls, connects
    s.space_id, s.root, s.home = space_id, project, home

    async def open_app():
        stack = AsyncExitStack()
        app = main.create_app(str(project))
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False))
        scope = (await http.post("/api/projects/open", json={"path": str(project)})).json()
        s.app, s.http, s.stack = app, http, stack
        s.ctx = app.state.project_registry[scope["project_id"]].ctx
        s.base = f"/api/projects/{scope['project_id']}/remote"

    async def restart():
        # VS-188: a new backend on the same disk/home; the Remote stays up.
        await s.stack.aclose()
        await open_app()

    s.restart = restart
    await open_app()
    try:
        yield s
    finally:
        await s.stack.aclose()
        store.close()


def write_v1_base(s, *, space_id=None, url=URL, cursor=2, docs=None) -> Path:
    """The old runner's checkpoint for THIS root, written by the old class itself."""
    from voxarium_studio_remote.protocol import content_hash
    path = SyncState.file_for(s.ctx.paths.root)
    if docs is None:
        docs = {p: Base(rev, content_hash(c)) for rev, (p, c) in enumerate(DOCS, 1)}
    SyncState(path=path, url=url, space_id=space_id or s.space_id, cursor=cursor, docs=docs).save()
    assert path.is_file()
    return path


def disk(s) -> dict[str, str]:
    """Every byte a migration step could publish: project config, home stores, bases."""
    result = {}
    config = s.root / ".voxarium_studio" / "config.yaml"
    result[str(config)] = hashlib.sha256(config.read_bytes()).hexdigest()
    for path in sorted(user_config_dir().rglob("*")):
        if path.is_file() and path.name != "recent-projects.json" and path.suffix != ".lock":
            result[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def project_and_bases(s) -> dict[str, str]:
    """Only the project config, the private bindings and every (v1/v2) base."""
    bases = (user_config_dir() / "sync").as_posix()
    config = str(s.root / ".voxarium_studio" / "config.yaml")
    return {k: v for k, v in disk(s).items()
            if k == config or "remote-bindings" in k or Path(k).as_posix().startswith(bases)}


def config_raw(s) -> dict:
    return yaml.safe_load((s.root / ".voxarium_studio" / "config.yaml").read_text(encoding="utf-8"))


def refused(response, status, code, details=None):
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"code": code, "details": details or {}}
    secret_free(response.text)


async def plan(s) -> dict:
    response = await s.http.get(s.base + "/migration")
    assert response.status_code == 200, response.text
    return response.json()


async def profile_command(s, shown=None, **body):
    shown = shown or await plan(s)
    payload = {"config_fingerprint": shown["config_fingerprint"],
               "import_legacy_credential": False, "confirmation": {"url": shown["legacy_url"]}}
    payload.update(body)
    if "name" not in payload and "profile_id" not in payload:
        payload["profile_id"] = PROFILE
    return await s.http.post(f"{s.base}/migration/{shown['migration_plan_id']}/profile", json=payload)


async def resolve(s, shown=None, **body):
    shown = shown or await plan(s)
    payload = {"profile_id": PROFILE, "expected_profile_revision": 1,
               "config_fingerprint": shown["config_fingerprint"], **body}
    return await s.http.post(f"{s.base}/migration/{shown['migration_plan_id']}/resolve", json=payload)


async def prepare(s, shown, resolved, **body):
    payload = {"expected_revision": 0, "config_fingerprint": shown["config_fingerprint"],
               "persistent_id": resolved["catalog_id"], "operation": "migrate",
               "profile_id": resolved["profile_id"], "url": resolved["url"],
               "catalog_id": resolved["catalog_id"],
               "catalog_instance_id": resolved["space_instance_id"],
               "migration_plan_id": shown["migration_plan_id"], **body}
    return await s.http.post(s.base + "/prepare", json=payload)


async def prepared_change(s):
    shown = await plan(s)
    response = await resolve(s, shown)
    assert response.status_code == 200, response.text
    resolved = response.json()
    response = await prepare(s, shown, resolved)
    assert response.status_code == 200, response.text
    prepared = response.json()
    change = f"{s.base}/changes/{prepared['change_id']}"
    gate = s.ctx.docs.transitions
    response = await s.http.post(change + "/quiesce", json={"expected_generation": gate.generation})
    assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    body = {"expected_revision": 0, "expected_generation": gate.generation,
            "config_fingerprint": shown["config_fingerprint"],
            "confirmation": {"url": URL, "persistent_id": resolved["catalog_id"]}}
    return change, body, shown, resolved


# ------------------------------------------------------------------ nothing on reads


async def test_nothing_migrates_on_get_startup_or_any_read(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        base = write_v1_base(s)
        before = disk(s)
        legacy_bytes = base.read_bytes()
        shown = await plan(s)
        assert shown["state"] == "needs_profile" and shown["legacy_url"] == URL
        assert shown["legacy_selector"] == NAME and shown["persistent_id"] is None
        observed = (await s.http.get(s.base)).json()
        assert observed["state"] == "migration_required" and observed["persistent_id"] is None
        assert observed["profile_id"] is None and observed["space_instance_id"] is None
        project_id = s.base.split("/")[3]
        for path in (s.base, s.base + "/migration", f"/api/projects/{project_id}/sync",
                     f"/api/projects/{project_id}/project", "/api/project"):
            assert (await s.http.get(path)).status_code == 200, path
        assert disk(s) == before and base.read_bytes() == legacy_bytes
        assert s.calls == [] and s.connects == []
        assert "project_identity" not in config_raw(s)
        assert config_raw(s)["sync"]["space"] == RAW_NAME


async def test_restart_does_not_migrate(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        write_v1_base(s)
        before = disk(s)
    async with AsyncExitStack() as stack:
        app = main.create_app(str(project))
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False))
        scope = (await http.post("/api/projects/open", json={"path": str(project)})).json()
        shown = (await http.get(f"/api/projects/{scope['project_id']}/remote/migration")).json()
        assert shown["state"] == "needs_profile"
    after = {k: v for k, v in disk(s).items() if k in before}
    assert after == before


# ------------------------------------------------------------------ profile command


async def test_profile_command_creates_once_then_reuses_the_exact_url(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home, profile=False) as s:
        base = write_v1_base(s)
        guarded = project_and_bases(s)
        legacy_bytes = base.read_bytes()
        response = await profile_command(s, name="Team Remote")
        assert response.status_code == 200, response.text
        created = response.json()
        assert set(created) == {"migration_plan_id", "profile_id", "profile_revision", "url", "credential"}
        assert created["url"] == URL and created["credential"] == "missing"
        secret_free(response.text)
        # A second create of the same URL names the existing profile, never a second one.
        refused(await profile_command(s, name="Other name"), 409, "profile_url_collision",
                {"profile_id": created["profile_id"]})
        again = await profile_command(s, profile_id=created["profile_id"])
        assert again.status_code == 200 and again.json() == created
        from voxarium_studio.sync.profiles import RemoteProfiles
        assert [p.url for p in RemoteProfiles().list()] == [URL]
        # Personal store only: no project config/base write, no network, no socket.
        assert project_and_bases(s) == guarded and base.read_bytes() == legacy_bytes
        assert s.calls == [] and s.connects == []


async def test_profile_import_is_refused_by_the_c7_guard_in_production(project, monkeypatch, studio_home):
    """Gate L: no positive legacy completeness owner, so the first key write refuses."""
    async with migration_studio(project, monkeypatch, studio_home, stored=None) as s:
        before = disk(s)
        refused(await profile_command(s, import_legacy_credential=True), 503,
                "consumer_lifecycle_unavailable", {"profile_id": PROFILE})
        assert disk(s) == before and s.calls == [] and s.connects == []


async def test_profile_import_through_a_known_inventory_is_idempotent(project, monkeypatch, studio_home):
    from voxarium_studio.sync.profile_lifecycle import BindingInventory
    async with migration_studio(project, monkeypatch, studio_home, stored=None) as s:
        guarded = project_and_bases(s)
        # The documented test/future-owner seam of the SAME C7 guard; not weakened.
        s.app.state.remote_profile_lifecycle._inventory = lambda pid: BindingInventory(
            state="known", drained=True)
        response = await profile_command(s, import_legacy_credential=True)
        assert response.status_code == 200, response.text
        imported = response.json()
        assert imported["credential"] == "stored" and imported["profile_revision"] == 2
        assert credentials.load_profile_token(PROFILE, URL) == KEY_B
        raw = json.loads((user_config_dir() / "remote-keys.json").read_text(encoding="utf-8"))
        assert raw["legacy"] == {URL: KEY_B}  # The legacy key is kept.
        keys = (user_config_dir() / "remote-keys.json").read_bytes()
        again = await profile_command(s, import_legacy_credential=True)
        assert again.status_code == 200 and again.json() == imported
        assert (user_config_dir() / "remote-keys.json").read_bytes() == keys
        assert project_and_bases(s) == guarded and s.calls == [] and s.connects == []


async def test_profile_import_never_overwrites_a_different_key(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home, stored=KEY_A, legacy=KEY_B) as s:
        before = disk(s)
        refused(await profile_command(s, import_legacy_credential=True), 409,
                "credential_conflict", {"profile_id": PROFILE})
        assert disk(s) == before and credentials.load_profile_token(PROFILE, URL) == KEY_A


async def test_profile_command_requires_the_exact_plan_and_url(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        shown = await plan(s)
        before = disk(s)
        refused(await profile_command(s, shown, confirmation={"url": RAW_URL}), 409, "confirmation_required")
        refused(await profile_command(s, shown, confirmation={"url": "http://127.0.0.1:8788"}),
                409, "confirmation_required")
        refused(await profile_command(s, shown, config_fingerprint="0" * 64), 409, "migration_plan_stale")
        stale = {**shown, "migration_plan_id": "1" * 64}
        refused(await profile_command(s, stale), 409, "migration_plan_stale")
        refused(await profile_command(s, shown, name="Both", profile_id=PROFILE), 422, "invalid_request")
        refused(await profile_command(s, shown, import_legacy_credential="yes"), 422, "invalid_request")
        refused(await profile_command(s, shown, url=URL), 422, "invalid_request")
        refused(await profile_command(s, shown, profile_id=str(uuid.uuid4())), 404, "profile_not_found")
        assert disk(s) == before and s.calls == [] and s.connects == []


async def test_profile_of_another_url_is_not_reused(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        config = yaml.safe_load((user_config_dir() / "config.yaml").read_text(encoding="utf-8"))
        config["remote_servers"][0]["url"] = "http://127.0.0.1:8788"
        (user_config_dir() / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
        before = disk(s)
        refused(await profile_command(s), 409, "profile_url_mismatch", {"profile_id": PROFILE})
        refused(await resolve(s), 409, "profile_url_mismatch", {"profile_id": PROFILE})
        assert disk(s) == before and s.calls == []


# ------------------------------------------------------------------ resolve


async def test_resolve_returns_the_exact_old_uuid_read_only(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        write_v1_base(s)
        before = disk(s)
        response = await resolve(s)
        assert response.status_code == 200, response.text
        secret_free(response.text)
        resolved = response.json()
        assert resolved["catalog_id"] == s.space_id == resolved["space_instance_id"]
        assert resolved["legacy_selector"] == NAME and resolved["display_name"] == NAME
        assert resolved["profile_id"] == PROFILE and resolved["url"] == URL
        assert resolved["conflicts"] == []
        kinds = {change["kind"]: change for change in resolved["changes"]}
        assert kinds["identity"] == {"kind": "identity", "from": None, "to": s.space_id, "revision": 1}
        assert kinds["remove_legacy_selector"]["value"] == NAME
        assert kinds["base"] == {"kind": "base", "state": "import", "documents": 2, "cursor": 2}
        assert "disable" not in kinds
        assert disk(s) == before and s.connects == []
        assert s.calls and all(method == "GET" for method, _ in s.calls), s.calls
        assert ("GET", "/api/v2/legacy-spaces/resolve") in s.calls
        assert not any(path.startswith("/api/v1/") for _, path in s.calls)


async def test_resolve_refusals_write_nothing(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        before = disk(s)
        refused(await resolve(s, expected_profile_revision=2), 409, "profile_revision_conflict",
                {"profile_id": PROFILE})
        refused(await resolve(s, profile_id=str(uuid.uuid4())), 404, "profile_not_found")
        refused(await resolve(s, config_fingerprint="0" * 64), 409, "migration_plan_stale")
        holder = s.app.state.remote_catalog

        async def offline(*args, **kwargs):
            raise CatalogError("transport_unavailable")

        holder.resolve_legacy = offline
        refused(await resolve(s), 503, "catalog_unavailable")
        del holder.resolve_legacy
        assert disk(s) == before and s.connects == []


async def test_resolve_refuses_a_missing_mapping(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home, space="Unknown") as s:
        before = disk(s)
        refused(await resolve(s), 409, "legacy_space_not_found")
        assert disk(s) == before and s.connects == []
        assert ("GET", "/api/v2/legacy-spaces/resolve") in s.calls


async def test_resolve_refuses_a_mapping_whose_id_is_not_its_epoch(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        native = s.store.create_project("native-id", "Native")
        assert native.storage_epoch != native.id
        s.store._db.execute("UPDATE legacy_spaces SET project_id=? WHERE name=?", ("native-id", NAME))
        before = disk(s)
        refused(await resolve(s), 409, "legacy_mapping_mismatch")
        assert disk(s) == before and s.connects == []


async def test_resolve_requires_a_usable_credential(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home, stored=None) as s:
        before = disk(s)
        refused(await resolve(s), 409, "credential_required", {"profile_id": PROFILE})
        assert disk(s) == before and s.calls == []


async def test_malformed_base_and_config_are_refused_without_repair(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        broken = SyncState.file_for(s.ctx.paths.root)
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(b'{"version": 1, "url": "x"')
        before = disk(s)
        refused(await resolve(s), 409, "legacy_base_invalid")
        assert disk(s) == before
        config = s.root / ".voxarium_studio" / "config.yaml"
        config.write_bytes(config.read_bytes() + b"\nsync: [not, a, mapping\n")
        shown = await plan(s)
        assert shown["state"] == "invalid_config"
        before = disk(s)
        refused(await resolve(s, shown), 409, "invalid_config")
        refused(await profile_command(s, shown, confirmation={"url": URL}), 409, "invalid_config")
        assert disk(s) == before and s.connects == []


# ------------------------------------------------------------------ apply


async def test_apply_commits_disabled_identity_binding_and_proven_base(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        legacy_path = write_v1_base(s)
        legacy_bytes = legacy_path.read_bytes()
        name_before = config_raw(s)["project_name"]
        change, body, shown, resolved = await prepared_change(s)
        response = await s.http.post(change + "/commit", json=body)
        assert response.status_code == 200, response.text
        secret_free(response.text)
        result = response.json()
        assert result["persistent_id"] == s.space_id and result["identity_revision"] == 1
        assert result["enabled"] is False and result["state"] == "disabled"
        assert result["profile_id"] == PROFILE and result["space_instance_id"] == s.space_id
        assert result["migration"] == {"migration_plan_id": shown["migration_plan_id"],
                                       "legacy_selector": NAME, "base": "imported"}
        raw = config_raw(s)
        assert raw["project_identity"] == {"id": s.space_id, "revision": 1}
        assert raw["sync"] == {"enabled": False, "url": URL}  # Only sync.space is gone.
        assert raw["project_name"] == name_before
        from voxarium_studio.sync.bindings import RemoteBindings
        row = RemoteBindings().read().get(s.root)
        assert (row.selection_state, row.persistent_id, row.identity_revision, row.profile_id,
                row.url, row.space_instance_id) == ("selected", s.space_id, 1, PROFILE, URL, s.space_id)
        checkpoint = SyncStateV2.load(s.ctx.paths.root, url=URL, persistent_id=s.space_id)
        legacy = SyncState.load(s.ctx.paths.root, url=URL, space_id=s.space_id)
        assert checkpoint.space_instance_id == s.space_id and checkpoint.cursor == legacy.cursor == 2
        assert checkpoint.docs == legacy.docs
        assert legacy_path.read_bytes() == legacy_bytes  # The old state is kept.
        observed = (await s.http.get(s.base)).json()
        assert (observed["persistent_id"], observed["profile_id"], observed["space_instance_id"],
                observed["enabled"]) == (s.space_id, PROFILE, s.space_id, False)
        assert (await plan(s))["state"] == "identity_present"
        # Never Enable, never a socket; every Remote request was a GET.
        assert s.connects == [] and s.ctx.sync._runner is None
        assert all(method == "GET" for method, _ in s.calls), s.calls
        # Idempotent: the same command returns its historical receipt, writes nothing.
        after = disk(s)
        again = await s.http.post(change + "/commit", json=body)
        assert again.status_code == 200, again.text
        assert {k: again.json()[k] for k in ("persistent_id", "identity_revision", "config_fingerprint",
                                             "binding_generation", "migration")} == {
            k: result[k] for k in ("persistent_id", "identity_revision", "config_fingerprint",
                                   "binding_generation", "migration")}
        # A repeated migration of the already migrated root is refused, not re-applied:
        # the old plan is bound to the old bytes, and a fresh plan is not a legacy one.
        refused(await prepare(s, shown, resolved), 409, "migration_plan_stale")
        refused(await profile_command(s, shown), 409, "migration_plan_stale")
        fresh = await plan(s)
        refused(await profile_command(s, fresh, confirmation={"url": URL}), 409,
                "migration_not_applicable")
        refused(await resolve(s, fresh), 409, "migration_not_applicable")
        assert disk(s) == after
        # The imported base is the Preview's matched base for the same incarnation.
        preview = await s.http.post(s.base + "/reconcile-preview", json={
            "expected_revision": 1, "config_fingerprint": observed["config_fingerprint"]})
        assert preview.status_code == 200, preview.text
        assert preview.json()["base"] == "matched"
        assert s.connects == []


@pytest.mark.parametrize("variant", ["other_incarnation", "other_url", "ahead_cursor", "none"])
async def test_apply_without_proof_keeps_the_old_base_unimported(project, monkeypatch, studio_home, variant):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        legacy_path = None
        if variant == "other_incarnation":
            legacy_path = write_v1_base(s, space_id=uuid.uuid4().hex)
        elif variant == "other_url":
            legacy_path = write_v1_base(s, url="http://127.0.0.1:8788")
        elif variant == "ahead_cursor":
            legacy_path = write_v1_base(s, cursor=99)
        legacy_bytes = legacy_path.read_bytes() if legacy_path else None
        change, body, _, resolved = await prepared_change(s)
        expected = "none" if variant == "none" else "unproven"
        kinds = {c["kind"]: c for c in resolved["changes"]}
        assert kinds["base"]["state"] == expected
        response = await s.http.post(change + "/commit", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["migration"]["base"] == expected
        assert SyncStateV2.load(s.ctx.paths.root, url=URL, persistent_id=s.space_id) is None
        if legacy_path is not None:
            assert legacy_path.read_bytes() == legacy_bytes
        assert config_raw(s)["project_identity"] == {"id": s.space_id, "revision": 1}


async def test_enabled_legacy_is_drained_and_committed_disabled(project, monkeypatch, studio_home):
    # No legacy URL key: the old runner is configured but never connects.
    async with migration_studio(project, monkeypatch, studio_home, enabled=True, legacy=None) as s:
        assert s.ctx.config.sync.enabled and s.ctx.sync._runner is None
        change, body, _, resolved = await prepared_change(s)
        assert {"kind": "disable", "enabled": False} in resolved["changes"]
        response = await s.http.post(change + "/commit", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["enabled"] is False
        assert config_raw(s)["sync"] == {"enabled": False, "url": URL}
        assert s.connects == [] and s.ctx.sync._runner is None


async def test_identity_equal_to_selector_advances_its_revision(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home, space="boloto",
                                identity={"id": "boloto", "revision": 4}) as s:
        s.store._db.execute("UPDATE legacy_spaces SET name=? WHERE name=?", ("boloto", NAME))
        shown = await plan(s)
        assert shown["state"] == "needs_profile" and shown["identity_revision"] == 4
        resolved = (await resolve(s, shown)).json()
        response = await prepare(s, shown, resolved, expected_revision=4)
        assert response.status_code == 200, response.text
        change = f"{s.base}/changes/{response.json()['change_id']}"
        gate = s.ctx.docs.transitions
        assert (await s.http.post(change + "/quiesce",
                                  json={"expected_generation": gate.generation})).json()["state"] == "ready"
        response = await s.http.post(change + "/commit", json={
            "expected_revision": 4, "expected_generation": gate.generation,
            "config_fingerprint": shown["config_fingerprint"],
            "confirmation": {"url": URL, "persistent_id": s.space_id}})
        assert response.status_code == 200, response.text
        assert config_raw(s)["project_identity"] == {"id": s.space_id, "revision": 5}


# ------------------------------------------------------------------ conflicts write nothing


async def test_prepare_requires_the_current_resolution(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        shown = await plan(s)
        fake = {"catalog_id": s.space_id, "space_instance_id": s.space_id, "profile_id": PROFILE, "url": URL}
        before = disk(s)
        refused(await prepare(s, shown, fake), 409, "resolution_required")
        resolved = (await resolve(s, shown)).json()
        refused(await prepare(s, shown, resolved, catalog_instance_id=uuid.uuid4().hex), 409,
                "resolution_stale")
        refused(await prepare(s, shown, resolved), 409, "resolution_required")  # Discarded.
        resolved = (await resolve(s, shown)).json()
        refused(await prepare(s, shown, resolved, expected_revision=1), 409, "stale_binding")
        refused(await prepare(s, shown, resolved, migration_plan_id=None), 422, "invalid_request")
        # Only migrate may name revision 0; it is never relabelled as a Select.
        refused(await prepare(s, shown, resolved, operation="select"), 422, "invalid_request")
        assert disk(s) == before and s.connects == []


async def test_duplicate_local_binding_writes_nothing(project, monkeypatch, studio_home, tmp_path):
    from voxarium_studio.core.application_config import RemoteServerProfile
    from voxarium_studio.core.project_identity import ProjectIdentity
    from voxarium_studio.sync.bindings import BindingRecord, RemoteBindings
    async with migration_studio(project, monkeypatch, studio_home) as s:
        legacy_path = write_v1_base(s)
        other = tmp_path / "copy"
        other.mkdir()
        RemoteBindings().save(BindingRecord.for_root(
            other, profile=RemoteServerProfile(profile_id=PROFILE, name="Team", url=URL, revision=1),
            identity=ProjectIdentity(id=s.space_id, revision=1), selection_state="selected",
            space_instance_id=s.space_id), expected_fingerprint=RemoteBindings().read().fingerprint)
        before = disk(s)
        shown = await plan(s)
        response = await resolve(s, shown)
        assert response.status_code == 200, response.text
        resolved = response.json()
        assert resolved["conflicts"] == [{"code": "duplicate_local_binding"}]
        refused(await prepare(s, shown, resolved), 409, "duplicate_local_binding")
        assert disk(s) == before and legacy_path.is_file() and s.connects == []


async def test_identity_selector_conflict_writes_nothing(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home,
                                identity={"id": "another-id", "revision": 2}) as s:
        shown = await plan(s)
        assert shown["state"] == "migration_conflict"
        before = disk(s)
        refused(await profile_command(s, shown), 409, "migration_conflict")
        refused(await resolve(s, shown), 409, "migration_conflict")
        assert disk(s) == before and s.calls == [] and s.connects == []


async def test_commit_rereads_the_resolution_and_refuses_a_changed_mapping(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        write_v1_base(s)
        change, body, _, _ = await prepared_change(s)
        guarded = project_and_bases(s)
        s.store._db.execute("DELETE FROM legacy_spaces WHERE name=?", (NAME,))
        refused(await s.http.post(change + "/commit", json=body), 409, "stale_binding")
        assert project_and_bases(s) == guarded
        assert "project_identity" not in config_raw(s) and config_raw(s)["sync"]["space"] == RAW_NAME
        assert SyncStateV2.load(s.ctx.paths.root, url=URL, persistent_id=s.space_id) is None
        cancelled = await s.http.delete(change)
        assert cancelled.status_code == 200, cancelled.text
        assert s.connects == []


async def test_commit_refuses_a_base_changed_after_resolve(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        write_v1_base(s)
        change, body, _, _ = await prepared_change(s)
        write_v1_base(s, cursor=1, docs={"a.md": Base(1, None)})
        guarded = project_and_bases(s)
        refused(await s.http.post(change + "/commit", json=body), 409, "stale_binding")
        assert project_and_bases(s) == guarded and "project_identity" not in config_raw(s)


async def test_migrated_root_restarts_disabled_without_a_socket(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        legacy_path = write_v1_base(s)
        legacy_bytes = legacy_path.read_bytes()
        change, body, _, _ = await prepared_change(s)
        assert (await s.http.post(change + "/commit", json=body)).status_code == 200
        space_id, connects = s.space_id, s.connects
    async with AsyncExitStack() as stack:
        app = main.create_app(str(project))
        await stack.enter_async_context(app.router.lifespan_context(app))
        http = await stack.enter_async_context(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1", trust_env=False))
        scope = (await http.post("/api/projects/open", json={"path": str(project)})).json()
        base = f"/api/projects/{scope['project_id']}/remote"
        observed = (await http.get(base)).json()
        assert (observed["persistent_id"], observed["identity_revision"], observed["enabled"],
                observed["profile_id"], observed["space_instance_id"]) == (
            space_id, 1, False, PROFILE, space_id)
        assert observed["state"] != "connected"
        assert (await http.get(base + "/migration")).json()["state"] == "identity_present"
    assert connects == [] and legacy_path.read_bytes() == legacy_bytes


# ------------------------------------------------------------------ VS-188: crash at every migrate write
#
# A process crash at each publication of the migrate commit, a restart, then
# the explicit POST …/recover: before the project config (the commit point)
# the legacy root returns to its previous, inert settings; from the config
# write on, the migrate commit is finished. Startup/GET never repair; the old
# v1 base and the stored key are never changed; a repeat is a receipt.


class Crash(BaseException):
    """The process dies here: no except/cleanup of the command runs."""


# (owner, phase, when) of the migrate commit's publications, in order.
MIGRATE_POINTS = [
    ("journal", "committing", "before"), ("journal", "committing", "after"),
    ("config", None, "after"), ("journal", "config_committed", "after"),
    ("private", None, "after"), ("checkpoint", None, "after"),
    ("journal", "completed", "before"), ("journal", "completed", "after"),
]
COMMIT_POINT = 2  # The project config replace.


def inject_migrate(mp, point):
    from voxarium_studio.core import config as config_owner
    from voxarium_studio.sync import transitions
    from voxarium_studio.sync.bindings import RemoteBindings

    owner, phase, when = point
    if owner == "journal":
        original = transitions.publish_binding_change_commit

        def journal(root, change_id, expected, commit, current):
            if current == phase and when == "before":
                raise Crash()
            row = original(root, change_id, expected, commit, current)
            if current == phase:
                raise Crash()
            return row

        mp.setattr(transitions, "publish_binding_change_commit", journal)
    elif owner == "config":
        original_write = config_owner._write_project_document_in_transaction

        def write(paths, payload, **kwargs):
            original_write(paths, payload, **kwargs)
            raise Crash()

        mp.setattr(config_owner, "_write_project_document_in_transaction", write)
    elif owner == "private":
        original_save = RemoteBindings._save_in_transaction

        def save(self, record, **kwargs):
            original_save(self, record, **kwargs)
            raise Crash()

        mp.setattr(RemoteBindings, "_save_in_transaction", save)
    else:
        original_state = SyncStateV2.save

        def save_state(self):
            original_state(self)
            raise Crash()

        mp.setattr(SyncStateV2, "save", save_state)


def recovery_body(observed):
    shown = observed["recovery"]
    return {"change_id": shown["change_id"], "expected_revision": observed["identity_revision"] or 0,
            "config_fingerprint": observed["config_fingerprint"],
            "confirmation": {"url": shown["target"]["url"],
                             "persistent_id": shown["target"]["persistent_id"], "action": "recover"},
            "backend_epoch": observed["backend_epoch"]}


def journal_rows(s):
    from voxarium_studio.sync.transitions import read_binding_change_journals
    return read_binding_change_journals(s.root)


def keys_bytes(s):
    return (s.home / "remote-keys.json").read_bytes()


def private_row(s):
    from voxarium_studio.sync.bindings import RemoteBindings
    return RemoteBindings().read().get(s.root)


async def crash_migrate(s, point):
    change, body, shown, resolved = await prepared_change(s)
    with pytest.MonkeyPatch.context() as mp:
        inject_migrate(mp, point)
        with pytest.raises(Crash):
            await s.http.post(change + "/commit", json=body)
    return shown, resolved


def assert_migrated(s, legacy_bytes):
    raw = config_raw(s)
    assert raw["project_identity"] == {"id": s.space_id, "revision": 1}
    assert raw["sync"] == {"enabled": False, "url": URL}
    row = private_row(s)
    assert (row.selection_state, row.persistent_id, row.identity_revision, row.space_instance_id) == (
        "selected", s.space_id, 1, s.space_id)
    checkpoint = SyncStateV2.load(s.ctx.paths.root, url=URL, persistent_id=s.space_id)
    legacy = SyncState.load(s.ctx.paths.root, url=URL, space_id=s.space_id)
    assert checkpoint.space_instance_id == s.space_id and checkpoint.cursor == legacy.cursor == 2
    assert checkpoint.docs == legacy.docs
    assert SyncState.file_for(s.ctx.paths.root).read_bytes() == legacy_bytes


@pytest.mark.parametrize("enabled", [False, True], ids=["legacy-off", "legacy-on"])
@pytest.mark.parametrize("point", MIGRATE_POINTS, ids=lambda p: "-".join(x for x in p if x))
async def test_crash_at_every_migrate_write_then_explicit_recovery(
        project, monkeypatch, studio_home, point, enabled):
    async with migration_studio(project, monkeypatch, studio_home, enabled=enabled,
                                legacy=None if enabled else KEY_B) as s:
        legacy_path = write_v1_base(s)
        legacy_bytes = legacy_path.read_bytes()
        legacy_config_bytes = (s.root / ".voxarium_studio" / "config.yaml").read_bytes()
        keys = keys_bytes(s)
        await crash_migrate(s, point)
        await s.restart()
        index = MIGRATE_POINTS.index(point)
        # Startup and GET classify only: nothing is written, nothing connects.
        settled = disk(s)
        observed = (await s.http.get(s.base)).json()
        again = (await s.http.get(s.base)).json()
        assert again["recovery"] == observed["recovery"]
        await s.restart()
        assert disk(s) == settled, "neither GET nor startup repairs"
        assert s.connects == [] and s.ctx.sync._runner is None
        assert legacy_path.read_bytes() == legacy_bytes and keys_bytes(s) == keys
        observed = (await s.http.get(s.base)).json()
        if point == ("journal", "completed", "after"):
            # Nothing left to recover: the migration is complete and disabled.
            assert observed["recovery"] is None and observed["state"] == "disabled"
            assert_migrated(s, legacy_bytes)
            return
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        shown = observed["recovery"]
        assert shown["available"] and shown["operation"] == "migrate"
        published = index >= COMMIT_POINT
        if published:
            assert shown["outcome"] == "commit"
            assert shown["target"] == {"persistent_id": s.space_id, "url": URL,
                                       "identity_revision": 1, "enabled": False}
        else:
            assert shown["outcome"] == "previous"
            assert shown["target"] == {"persistent_id": None, "url": URL,
                                       "identity_revision": 0, "enabled": False}
        body = recovery_body(observed)
        wrong = json.loads(json.dumps(body))
        wrong["confirmation"]["url"] = "http://127.0.0.1:8788"
        refused(await s.http.post(s.base + "/recover", json=wrong), 409, "confirmation_required")
        refused(await s.http.post(s.base + "/recover", json={**body, "config_fingerprint": "0" * 64}),
                409, "stale_binding")
        assert disk(s) == settled
        response = await s.http.post(s.base + "/recover", json=body)
        assert response.status_code == 200, response.text
        secret_free(response.text)
        result = response.json()
        assert result["recovered"] and result["enabled"] is False and result["outcome"] == shown["outcome"]
        assert s.connects == [] and s.ctx.sync._runner is None
        assert all(method == "GET" for method, _ in s.calls), s.calls
        # The old v1 base and the stored key are never touched.
        assert legacy_path.read_bytes() == legacy_bytes and keys_bytes(s) == keys
        (record,) = journal_rows(s)
        if published:
            assert record["phase"] == "completed" and record["version"] == 2
            assert result["state"] == "disabled" and result["persistent_id"] == s.space_id
            assert_migrated(s, legacy_bytes)
        else:
            assert record["phase"] == "recovered"
            assert result["state"] == "migration_required" and result["persistent_id"] is None
            raw = config_raw(s)
            assert "project_identity" not in raw and private_row(s) is None
            assert raw["sync"] == {"enabled": False, "url": RAW_URL, "space": RAW_NAME}
            if not enabled:
                # Already sync-off: only the record was advanced, the bytes are the old ones.
                assert (s.root / ".voxarium_studio" / "config.yaml").read_bytes() == legacy_config_bytes
            assert SyncStateV2.load(s.ctx.paths.root, url=URL, persistent_id=s.space_id) is None
        # Idempotent: the same command again writes nothing.
        after = disk(s)
        repeat = await s.http.post(s.base + "/recover", json=body)
        assert repeat.status_code == 200, repeat.text
        assert disk(s) == after
        await s.restart()
        observed = (await s.http.get(s.base)).json()
        assert observed["recovery"] is None and s.connects == []
        assert disk(s) == after
        if published:
            # The migrated root works: the imported base is the matched base of a Preview.
            assert observed["state"] == "disabled"
            preview = await s.http.post(s.base + "/reconcile-preview", json={
                "expected_revision": 1, "config_fingerprint": observed["config_fingerprint"]})
            assert preview.status_code == 200, preview.text
            assert preview.json()["base"] == "matched"
        else:
            # The legacy root is offered the migration again, which now succeeds.
            assert observed["state"] == "migration_required"
            change, commit_body, _, _ = await prepared_change(s)
            response = await s.http.post(change + "/commit", json=commit_body)
            assert response.status_code == 200, response.text
            assert response.json()["migration"]["base"] == "imported"
            assert_migrated(s, legacy_bytes)
        assert s.connects == [] and keys_bytes(s) == keys


@pytest.mark.parametrize("step", ["private", "checkpoint", "completed"])
async def test_crash_while_finishing_a_migrate_commit_is_finished_by_the_same_command(
        project, monkeypatch, studio_home, step):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        legacy_path = write_v1_base(s)
        legacy_bytes = legacy_path.read_bytes()
        await crash_migrate(s, ("config", None, "after"))
        await s.restart()
        observed = (await s.http.get(s.base)).json()
        body = recovery_body(observed)
        point = {"private": ("private", None, "after"), "checkpoint": ("checkpoint", None, "after"),
                 "completed": ("journal", "completed", "before")}[step]
        with pytest.MonkeyPatch.context() as mp:
            inject_migrate(mp, point)
            with pytest.raises(Crash):
                await s.http.post(s.base + "/recover", json=body)
        await s.restart()
        observed = (await s.http.get(s.base)).json()
        assert observed["recovery"]["outcome"] == "commit" and observed["recovery"]["available"]
        response = await s.http.post(s.base + "/recover", json=recovery_body(observed))
        assert response.status_code == 200, response.text
        assert_migrated(s, legacy_bytes)
        (record,) = journal_rows(s)
        assert record["phase"] == "completed"
        assert s.connects == []


async def test_migrate_record_with_a_foreign_config_stays_blocked(project, monkeypatch, studio_home):
    async with migration_studio(project, monkeypatch, studio_home) as s:
        write_v1_base(s)
        await crash_migrate(s, ("config", None, "after"))
        config = s.root / ".voxarium_studio" / "config.yaml"
        raw = yaml.safe_load(config.read_text(encoding="utf-8"))
        raw["project_identity"] = {"id": "someone-else", "revision": 3}
        config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        await s.restart()
        before = disk(s)
        observed = (await s.http.get(s.base)).json()
        assert (observed["state"], observed["reason"]) == ("blocked", "recovery_required")
        assert observed["recovery"]["available"] is False
        assert observed["recovery"]["code"] == "recovery_conflict"
        refused(await s.http.post(s.base + "/recover", json=recovery_body(observed)), 409,
                "recovery_conflict")
        assert disk(s) == before and s.connects == []

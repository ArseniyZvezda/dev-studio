"""Unmounted Remote REST contract: in-process ASGI, synthetic providers only.

No TestClient, application lifespan, HTTP adapter, personal store or listener.
Run a task-temp copy with --noconftest, plugin autoload disabled, explicit
pytest_asyncio.plugin, asyncio_mode=auto and --basetemp under TASK-124.
No project conftest or Studio lifecycle module is needed, even at collection.
The real auth dependency and real catalog/Verify services are exercised separately
from the deliberately small boundary spies (studio-design §5.1/§6).
"""
from __future__ import annotations

import asyncio
import builtins
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import inspect
import importlib.abc
import io
import os
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace

from fastapi import FastAPI
import httpx
from pydantic import ValidationError
import pytest

# Collection must not import production composition to install test guards.
# A finder denies transitive fresh imports; the per-test __import__ guard below
# also catches attempts to reuse a module already cached by another test suite.
_RUNTIME_MODULES = tuple(
    prefix + suffix
    for prefix in ("voxarium_studio", "_vs124_unit")
    for suffix in (".main", ".core.context", ".core.workspace", ".sync.service")
)
_COLLECTION_COUNTS = Counter(runtime_import=0)
_TOTAL_COUNTS = Counter()


def _is_runtime(name):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _RUNTIME_MODULES)


class _RuntimeImportDeny(importlib.abc.MetaPathFinder):
    def __init__(self, forbidden):
        self.forbidden = forbidden

    def find_spec(self, fullname, path=None, target=None):
        if _is_runtime(fullname):
            self.forbidden()


def _deny_collection_runtime():
    _COLLECTION_COUNTS["runtime_import"] += 1
    raise AssertionError("Remote REST collection attempted a runtime import")


# The public api/sync package initializers eagerly import the full runtime.
# Execute the unchanged owner source under a private package namespace instead:
# bypass only those composition initializers, never replace the tested modules.
# This also avoids polluting production package identities in a larger suite.
_unit_root = next(iter(importlib.util.find_spec("voxarium_studio").submodule_search_locations))
for _suffix in ("", ".api", ".sync"):
    _package = ModuleType("_vs124_unit" + _suffix)
    _package.__path__ = [str(Path(_unit_root) / _suffix.lstrip("."))]
    _package.__package__ = _package.__name__
    sys.modules[_package.__name__] = _package


_collection_guard = _RuntimeImportDeny(_deny_collection_runtime)
sys.meta_path.insert(0, _collection_guard)
try:
    from _vs124_unit.api.remote import (
        CheckingVerification, ServerProfile, UnavailableVerification, UncheckedVerification,
        VerificationDTO, VerifyInput,
        create_remote_router,
    )
    from _vs124_unit.core.application_config import RemoteServerProfile
    from _vs124_unit.core.security import AuthSettings, require_auth
    from _vs124_unit.sync.catalog import (
        CatalogError, CatalogPage, CatalogProject, CatalogResponse, CatalogUser,
    )
    from _vs124_unit.sync.catalog_service import CatalogService
    from _vs124_unit.sync.catalog_verification import (
        CatalogVerificationService, VerificationStatus,
    )
    from _vs124_unit.sync.profiles import ProfileError
finally:
    sys.meta_path.remove(_collection_guard)


PROFILE = "abcdefab-0000-4000-8000-000000000119"
OTHER = "abcdefab-0000-4000-8000-000000000120"
BASE = "https://catalog.invalid/deployment/prefix"
OTHER_BASE = "https://other.invalid/private"
KEY = "SYNTHETIC-REMOTE-KEY-CANARY-119"
STUDIO_KEY = "SYNTHETIC-STUDIO-AUTH-CANARY-119"
CANARY = "SYNTHETIC-UNTRUSTED-INPUT-CANARY-119"
ROOT = "/api/remote/servers"
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-11-15T12:00:00Z"
EPOCH = "0123456789abcdef0123456789abcdef"
USER = CatalogUser("user-a", "Synthetic user", "#a1b2c3")
PROJECT = CatalogProject("alpha", "Historical label", EPOCH, 1, STAMP)
STATUS = VerificationStatus("valid", STAMP, USER, 2, 2, None)


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """No socketpair (a TCP listener on Windows) and no cross-thread work."""

    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("REST tests must not use cross-thread I/O")

    def _loop_self_reading(self, *_args):
        pass


@pytest.fixture(scope="module")
def event_loop_policy():
    class SyntheticPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return SocketFreeLoop()

    return SyntheticPolicy()


@pytest.fixture
def synthetic_home(tmp_path, monkeypatch):
    # Standalone fixtures: no project conftest, real home or implicit temp root.
    source = Path(__file__).resolve()
    task_temp = next((p for p in source.parents if p.name == "TASK-124"),
                     source.parents[2] / ".voxarium_studio" / "temp" / "TASK-124")
    assert tmp_path.resolve().is_relative_to(task_temp.resolve())
    home = tmp_path / "synthetic-home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME",
                 "VOXARIUM_STUDIO_HOME", "CODEX_HOME"):
        monkeypatch.setenv(name, str(home))
    return home


@pytest.fixture(scope="module", autouse=True)
def isolation_report():
    yield
    assert _COLLECTION_COUNTS == Counter(runtime_import=0)
    assert not any(_TOTAL_COUNTS.values())
    print("\nVS-124 forbidden-I/O counters:", dict(_TOTAL_COUNTS),
          "collection:", dict(_COLLECTION_COUNTS))


@pytest.fixture(autouse=True)
def isolation(monkeypatch, synthetic_home):
    """Deny *and count* forbidden fallbacks, including swallowed exceptions.

    Local fixtures create synthetic homes first; these tests never read stores.
    Filesystem operations are also denied for the duration of each ASGI request.
    Lifecycle imports are denied, not imported just to patch their entry points.
    """
    counts = Counter(network=0, storage=0, runtime=0, runtime_import=0, filesystem=0,
                     verify=0, clock=0, credential=0, transport=0, cache_write=0)

    def deny(category):
        def forbidden(*_args, **_kwargs):
            counts[category] += 1
            pytest.fail("Remote REST attempted forbidden " + category)
        return forbidden

    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = name
        if level:
            resolved = importlib.util.resolve_name("." * level + name, globals["__package__"])
        if _is_runtime(resolved) or any(_is_runtime(resolved + "." + item) for item in (fromlist or ())):
            return deny("runtime_import")()
        return original_import(name, globals, locals, fromlist, level)

    import_guard = _RuntimeImportDeny(deny("runtime_import"))
    sys.meta_path.insert(0, import_guard)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    from _vs124_unit.core import application_config, config, paths
    from _vs124_unit.sync import credentials, profiles
    from _vs124_unit.sync.catalog import CatalogClient

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                 "create_connection", "socketpair"):
        monkeypatch.setattr(socket, name, deny("network"))
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, deny("network"))
    # Keep the class identity usable by asyncio's platform code, but prevent all
    # fresh sockets, even if a new implementation bypasses connect/bind guards.
    original_socket = socket.socket

    class DeniedSocket(original_socket):
        def __new__(cls, *_args, **_kwargs):
            return deny("network")()

    monkeypatch.setattr(socket, "socket", DeniedSocket)
    for module, names in (
        (application_config, ("application_config_file", "load_application_config",
                              "_load_application_config_strict", "save_application_config",
                              "_write_application_profiles", "application_config_transaction")),
        (profiles, ("_load_application_config_strict", "_write_application_config",
                    "application_config_transaction", "application_config_file")),
        (credentials, ("keys_file", "_read_store", "_write_store", "_mutation", "_read",
                       "load_token", "save_token", "load_profile_token", "save_profile_token",
                       "profile_token_revision", "profile_token_source", "import_legacy_token",
                       "preflight_legacy_import", "_environment")),
        (paths, ("user_config_dir",)),
        (config, ("load_config", "save_config")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, deny("storage"))
    for name in ("__init__", "get", "list", "create", "rename", "delete", "change_url",
                 "set_credential", "clear_credential", "load_token", "recover",
                 "import_legacy_credential"):
        monkeypatch.setattr(profiles.RemoteProfiles, name, deny("storage"))
    monkeypatch.setattr(Path, "home", deny("storage"))
    monkeypatch.setattr(CatalogClient, "create_project", deny("runtime"))
    for name in ("VOXARIUM_STUDIO_REMOTE_URL", "VOXARIUM_STUDIO_REMOTE_TOKEN",
                 "VOXARIUM_STUDIO_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield SimpleNamespace(counts=counts, deny=deny)
    finally:
        sys.meta_path.remove(import_guard)
        _TOTAL_COUNTS.update(counts)
    assert not any(counts.values())


class Metadata:
    def __init__(self, *items):
        self.items = tuple(items)
        self.calls = []
        self.error = None

    def list(self):
        self.calls.append(())
        if self.error:
            raise self.error
        return self.items


class Profiles:
    def __init__(self):
        self.items = {PROFILE: RemoteServerProfile(profile_id=PROFILE, name="Synthetic", url=BASE)}
        self.calls = []
        self.error = None

    def get(self, profile_id):
        self.calls.append(profile_id)
        if self.error:
            raise self.error
        if profile_id not in self.items:
            raise ProfileError("profile_not_found")
        return self.items[profile_id]

    def change(self, **changes):
        self.items[PROFILE] = self.items[PROFILE].model_copy(update=changes)


class VerifySpy:
    def __init__(self):
        self.calls = []
        self.result = STATUS
        self.error = None

    async def verify(self, profile_id, *, expected_revision):
        self.calls.append((profile_id, expected_revision))
        if self.error:
            raise self.error
        return self.result


class CatalogSpy:
    def __init__(self):
        self.calls = []
        self.result = CatalogPage((PROJECT,), "alpha")
        self.error = None
        self.hook = None

    async def list_projects(self, profile_id, *, expected_revision, after):
        self.calls.append((profile_id, expected_revision, after))
        await asyncio.sleep(0)
        if self.hook:
            self.hook()
        if self.error:
            raise self.error
        return self.result


def metadata_item(**changes):
    return ServerProfile(profile_id=PROFILE, name="Synthetic", url=BASE, revision=1,
                         has_token=True, credential_source="stored",
                         verification=UncheckedVerification(state="unchecked")).model_copy(update=changes)


def boundary(*, auth=None):
    metadata, profiles, verification, catalog = Metadata(metadata_item()), Profiles(), VerifySpy(), CatalogSpy()
    router = create_remote_router(metadata=metadata, profiles=profiles,
                                  verification=verification, catalog=catalog)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.auth = auth or AuthSettings()
    app.include_router(router)
    return SimpleNamespace(app=app, router=router, metadata=metadata, profiles=profiles,
                           verification=verification, catalog=catalog)


async def request(env, isolation, method="GET", suffix="", *, routed=True, **kwargs):
    # Warm the strictly in-process client before forbidding filesystem access.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app),
                                base_url="http://127.0.0.1", trust_env=False,
                                follow_redirects=False) as client:
        with pytest.MonkeyPatch.context() as patch:
            for owner, names in (
                (builtins, ("open",)), (io, ("open",)),
                (os, ("open", "mkdir", "unlink", "remove", "rename", "replace", "rmdir")),
                (Path, ("read_text", "read_bytes", "write_text", "write_bytes", "touch")),
            ):
                for name in names:
                    def forbidden_io(*args, _name=name, **kwargs):
                        if _name not in {"read_text", "read_bytes"}:
                            isolation.counts["cache_write"] += 1
                        return isolation.deny("filesystem")(*args, **kwargs)
                    patch.setattr(owner, name, forbidden_io)
            result = await client.request(method, ROOT + suffix, **kwargs)
    if routed:
        assert result.headers["Cache-Control"] == "no-store"
    for canary in (KEY, STUDIO_KEY, CANARY):
        assert canary not in result.text
        assert canary not in repr(dict(result.headers))
    return result


def error(response, code, status):
    assert response.status_code == status
    assert response.json() == {"code": code, "details": {}}


def idle(env):
    assert env.metadata.calls == env.profiles.calls == env.verification.calls == env.catalog.calls == []


def test_factory_requires_every_injection_and_exact_unmounted_surface():
    signature = inspect.signature(create_remote_router)
    required = {"metadata", "profiles", "verification", "catalog"}
    assert set(signature.parameters) == required | {"mutations", "creation", "consumers"}
    assert signature.parameters["consumers"].default is None
    assert signature.parameters["mutations"].default is None
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for name, p in signature.parameters.items() if name in required)
    for missing in required:
        supplied = {name: object() for name in required if name != missing}
        with pytest.raises(TypeError):
            create_remote_router(**supplied)
    env = boundary()
    assert {(r.path, tuple(sorted(r.methods))) for r in env.router.routes} == {
        (ROOT, ("GET",)), (ROOT + "/{profile_id}/verify", ("POST",)),
        (ROOT + "/{profile_id}/projects", ("GET",)),
    }
    # Newer FastAPI keeps an included router as one lazy aggregate; the
    # standalone router above is the authoritative route surface.
    assert not env.app.dependency_overrides
    assert env.app.openapi_url is env.app.docs_url is env.app.redoc_url is None
    assert all(r.dependant.dependencies[0].call is require_auth for r in env.router.routes)
    idle(env)


async def test_servers_is_metadata_only_and_secret_free(isolation):
    env = boundary()
    env.metadata.items += (metadata_item(profile_id=OTHER, name="No credential", url=OTHER_BASE,
                                         has_token=False, credential_source="none"),)
    first = await request(env, isolation)
    second = await request(env, isolation)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"items": [v.model_dump(mode="json") for v in env.metadata.items]}
    assert set(first.json()["items"][0]) == {"profile_id", "name", "url", "revision", "has_token",
                                             "credential_source", "verification"}
    assert first.json()["items"][0]["verification"] == {"state": "unchecked"}
    assert env.metadata.calls == [(), ()]
    assert env.profiles.calls == env.verification.calls == env.catalog.calls == []


async def test_metadata_preserves_stored_labels_and_explicit_verification(isolation):
    env = boundary()
    raw = env.metadata.items[0].model_dump()
    raw.update(name="  legacy\nlabel  ", credential_source="environment",
               verification=VerificationDTO.model_validate(asdict(STATUS)).model_dump())
    env.metadata.items = (ServerProfile.model_validate(raw, context={"stored_profile_metadata": True}),)
    response = await request(env, isolation)
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == raw["name"]
    assert response.json()["items"][0]["verification"] == asdict(STATUS)
    assert env.verification.calls == []


async def test_metadata_checking_has_no_fabricated_result_or_implicit_verify(isolation):
    env = boundary()
    env.metadata.items = (metadata_item(verification=CheckingVerification(state="checking")),)
    response = await request(env, isolation)
    assert response.status_code == 200
    assert response.json()["items"][0]["verification"] == {"state": "checking"}
    assert env.profiles.calls == env.verification.calls == env.catalog.calls == []


@pytest.mark.parametrize("source,has_token", [("stored", True), ("environment", True), ("none", False)])
async def test_unavailable_round_trips_repeated_get_without_verification_or_io(
        source, has_token, isolation, monkeypatch, caplog):
    # catalog-metadata-contract §§4–7: absence of a result is not unchecked
    # history or a checked failure, even after a key changes at the same source.
    env = real_boundary()
    value = metadata_item(credential_source=source, has_token=has_token,
                          verification=UnavailableVerification(state="unavailable"))
    value = ServerProfile.model_validate(value.model_dump())
    assert isinstance(value.verification, UnavailableVerification)
    env.metadata.items = (value,)
    before = value.model_dump(mode="json")
    monkeypatch.setattr(env.verification, "verify", isolation.deny("verify"))
    monkeypatch.setattr(env.verification, "_clock", isolation.deny("clock"))
    monkeypatch.setattr(Credentials, "__call__", isolation.deny("credential"))
    monkeypatch.setattr(env.transport, "request", isolation.deny("transport"))
    for key in (KEY, "SYNTHETIC-ROTATED-KEY-124"):
        env.credentials.key = key
        response = await request(env, isolation)
        assert response.status_code == 200
        assert response.json() == {"items": [before]}
        assert response.json()["items"][0]["verification"] == {"state": "unavailable"}
        assert key not in response.text and key not in repr(dict(response.headers))
        assert key not in caplog.text
    assert value.model_dump(mode="json") == before
    assert env.metadata.calls == [(), ()]
    assert env.credentials.calls == env.transport.calls == env.profiles.calls == []
    assert not any(isolation.counts.values())  # includes Verify/clock/cache-write


@pytest.mark.parametrize("field", ["checked_at", "user", "capabilities", "wire_protocol",
                                   "catalog_version", "error_code", "token"])
@pytest.mark.parametrize("value", [None, CANARY])
async def test_unavailable_forbids_every_checked_or_secret_extra_even_null(field, value, isolation, recwarn):
    raw = {"state": "unavailable", field: value}
    for model, payload in ((UnavailableVerification, raw),
                           (ServerProfile, {**metadata_item().model_dump(), "verification": raw})):
        with pytest.raises(ValidationError) as caught:
            model.model_validate(payload)
        assert CANARY not in str(caught.value)
    env = boundary()
    env.metadata.items = (metadata_item(verification=raw),)
    error(await request(env, isolation), "invalid_response", 502)
    assert env.verification.calls == env.catalog.calls == env.profiles.calls == []
    assert not recwarn.list


@pytest.mark.parametrize("bad", [None, "unavailable", 1, True, [], {}, {"state": None},
                                 {"state": CANARY}, {"state": 1}, {"state": True},
                                 {"state": []}, {"state": {}}, {"state": b"unavailable"}])
async def test_unknown_verification_never_defaults_to_unavailable_valid_or_unchecked(bad, isolation):
    with pytest.raises(ValidationError):
        UnavailableVerification.model_validate(bad)
    raw = {**metadata_item().model_dump(), "verification": bad}
    with pytest.raises(ValidationError) as caught:
        ServerProfile.model_validate(raw)
    assert CANARY not in str(caught.value)
    env = boundary()
    # A valid first row must not be published as a partial success.
    env.metadata.items = (metadata_item(verification=UnavailableVerification(state="unavailable")),
                          metadata_item(profile_id=OTHER, url=OTHER_BASE, verification=bad))
    error(await request(env, isolation), "invalid_response", 502)
    assert env.verification.calls == env.catalog.calls == env.profiles.calls == []


@pytest.mark.parametrize("state", [None, CANARY, 1, True, [], {}, b"unavailable", "valid", "unchecked", "checking"])
async def test_constructed_unavailable_is_revalidated_not_trusted(state, isolation, recwarn):
    corrupt = UnavailableVerification.model_construct(state=state)
    with pytest.raises(ValidationError):
        UnavailableVerification.model_validate(corrupt)
    # The nested union must not reinterpret this constructed unavailable model
    # as another branch (valid/unchecked/checking) based only on its bad state.
    with pytest.raises(ValidationError):
        ServerProfile.model_validate({**metadata_item().model_dump(), "verification": corrupt})
    # Unknown states/types also stay invalid after the router's plain snapshot
    # conversion. Explicit checking/unchecked dictionaries remain valid contracts.
    if state not in ("unchecked", "checking"):
        env = boundary()
        env.metadata.items = (metadata_item(verification=corrupt),)
        error(await request(env, isolation), "invalid_response", 502)
        assert env.verification.calls == env.catalog.calls == env.profiles.calls == []
    assert not recwarn.list


async def test_unavailable_discriminant_is_required_frozen_and_has_no_default(isolation):
    assert UnavailableVerification.model_fields["state"].is_required()
    assert inspect.signature(UnavailableVerification).parameters["state"].default is inspect.Parameter.empty
    assert UnavailableVerification.model_config["extra"] == "forbid"
    assert UnavailableVerification.model_config["strict"] is True
    assert UnavailableVerification.model_config["frozen"] is True
    assert UnavailableVerification.model_config["revalidate_instances"] == "always"
    assert UnavailableVerification.model_config["hide_input_in_errors"] is True
    value = UnavailableVerification(state="unavailable")
    with pytest.raises(ValidationError) as caught:
        value.state = "valid"
    assert caught.value.errors(include_input=False)[0]["type"] == "frozen_instance"
    assert value.model_dump() == {"state": "unavailable"}
    for raw in ({}, UnavailableVerification.model_construct()):
        with pytest.raises(ValidationError):
            UnavailableVerification.model_validate(raw)
    env = boundary()
    env.metadata.items = (metadata_item(verification=UnavailableVerification.model_construct()),)
    error(await request(env, isolation), "invalid_response", 502)
    missing = metadata_item().model_dump()
    del missing["verification"]
    with pytest.raises(ValidationError):
        ServerProfile.model_validate(missing)
    env.metadata.items = (ServerProfile.model_construct(**missing),)
    error(await request(env, isolation), "invalid_response", 502)
    assert env.verification.calls == env.catalog.calls == env.profiles.calls == []


@pytest.mark.parametrize("raw", [
    {"expected_revision": True}, {"expected_revision": "1"}, {"expected_revision": 1.0},
    {"expected_revision": 0}, {"expected_revision": -1}, {"expected_revision": None},
    {}, {"expected_revision": 1, "token": CANARY}, [], CANARY,
])
async def test_strict_verify_body_rejected_before_services(raw, isolation):
    env = boundary()
    response = await request(env, isolation, "POST", f"/{PROFILE}/verify", json=raw)
    error(response, "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("content", [b"{", b"null", b"", b'{"expected_revision": NaN}',
                                    ('{"token":"' + CANARY + '",').encode()])
async def test_malformed_or_absent_body_is_sanitized(content, isolation):
    env = boundary()
    response = await request(env, isolation, "POST", f"/{PROFILE}/verify", content=content,
                             headers={"Content-Type": "application/json"})
    error(response, "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("suffix", ["", f"/{PROFILE}/projects"])
async def test_get_rejects_unagreed_body_before_any_provider(suffix, isolation):
    env = boundary()
    error(await request(env, isolation, suffix=suffix, json={"token": CANARY}), "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("suffix,method,query", [
    ("", "GET", [("token", CANARY)]),
    ("", "GET", [("cursor", "alpha")]),
    (f"/{PROFILE}/verify", "POST", [("expected_revision", "1")]),
    (f"/{PROFILE}/verify", "POST", [("token", CANARY)]),
    (f"/{PROFILE}/projects", "GET", [("after", "alpha")]),
    (f"/{PROFILE}/projects", "GET", [("limit", "1")]),
    (f"/{PROFILE}/projects", "GET", [("expected_revision", "1")]),
    (f"/{PROFILE}/projects", "GET", [("cursor", "alpha"), ("cursor", "beta")]),
    (f"/{PROFILE}/projects", "GET", [("cursor", "alpha"), ("cursor", "alpha")]),
    (f"/{PROFILE}/projects", "GET", [("url", CANARY), ("url", CANARY)]),
])
async def test_query_extras_and_duplicates_fail_before_providers(suffix, method, query, isolation):
    env = boundary()
    kwargs = {"json": {"expected_revision": 1}} if method == "POST" else {}
    response = await request(env, isolation, method, suffix, params=query, **kwargs)
    error(response, "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("bad_id", [PROFILE.upper(), PROFILE.replace("-", ""), "{" + PROFILE + "}",
                                   "urn:uuid:" + PROFILE, "not-a-uuid", CANARY])
@pytest.mark.parametrize("action", ["verify", "projects"])
async def test_profile_ids_are_literal_canonical_uuids(bad_id, action, isolation):
    env = boundary()
    method = "POST" if action == "verify" else "GET"
    kwargs = {"json": {"expected_revision": 1}} if method == "POST" else {}
    response = await request(env, isolation, method, f"/{bad_id}/{action}", **kwargs)
    error(response, "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("cursor", ["", "Alpha", " alpha", "alpha ", "a/b", "a%2fb", "a?b",
                                  "a#b", "a" * 65, "é", CANARY])
async def test_cursor_is_canonical_literal_catalog_id(cursor, isolation):
    env = boundary()
    response = await request(env, isolation, suffix=f"/{PROFILE}/projects", params={"cursor": cursor})
    error(response, "invalid_request", 422)
    idle(env)


@pytest.mark.parametrize("cursor", [None, "0", "alpha", "a._-9", "a" * 64])
async def test_projects_captures_revision_and_aliases_pagination(cursor, isolation):
    env = boundary()
    env.profiles.change(revision=7)
    response = await request(env, isolation, suffix=f"/{PROFILE}/projects",
                             params={} if cursor is None else {"cursor": cursor})
    assert response.status_code == 200
    assert response.json() == {"items": [asdict(PROJECT)], "next_cursor": "alpha"}
    assert env.catalog.calls == [(PROFILE, 7, cursor)]
    assert env.profiles.calls == [PROFILE, PROFILE]
    assert env.metadata.calls == env.verification.calls == []


async def test_verify_uses_only_explicit_profile_revision_and_status(isolation):
    env = boundary()
    response = await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 9})
    assert response.status_code == 200 and response.json() == asdict(STATUS)
    assert env.verification.calls == [(PROFILE, 9)]
    assert env.metadata.calls == env.profiles.calls == env.catalog.calls == []


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", f"/{PROFILE}/verify"),
                                         ("GET", f"/{PROFILE}/projects")])
@pytest.mark.parametrize("headers,code,status", [
    ({}, "studio_unauthorised", 401),
    ({"Authorization": "Bearer " + CANARY}, "studio_unauthorised", 401),
    ({"Authorization": "Bearer " + STUDIO_KEY, "Origin": "https://evil.invalid"}, "studio_forbidden", 403),
    ({"Authorization": "Bearer " + STUDIO_KEY, "Origin": "null"}, "studio_forbidden", 403),
])
async def test_real_auth_rejects_without_provider_calls(method, suffix, headers, code, status, isolation):
    env = boundary(auth=AuthSettings(token=STUDIO_KEY))
    kwargs = {"json": {"expected_revision": 1}} if method == "POST" else {}
    response = await request(env, isolation, method, suffix, headers=headers, **kwargs)
    error(response, code, status)
    if status == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    idle(env)


async def test_real_auth_accepts_header_and_same_origin_but_blocks_loopback_rebinding(isolation):
    env = boundary(auth=AuthSettings(token=STUDIO_KEY))
    for headers in ({"Authorization": "Bearer " + STUDIO_KEY, "Origin": "http://127.0.0.1"},
                    {"X-Voxarium_Studio-Token": STUDIO_KEY}):
        assert (await request(env, isolation, headers=headers)).status_code == 200
    unauthenticated = boundary()
    error(await request(unauthenticated, isolation, headers={"Host": "evil.invalid"}),
          "studio_forbidden", 403)
    idle(unauthenticated)


@pytest.mark.parametrize("problem,code,status", [
    (CatalogError("profile_not_found"), "profile_not_found", 404),
    (CatalogError("profile_revision_conflict"), "profile_revision_conflict", 409),
    (CatalogError("credential_binding_conflict"), "credential_binding_conflict", 409),
    (CatalogError("unauthorised"), "remote_unauthorised", 502),
    (CatalogError("profile_recovery_required"), "profile_recovery_required", 409),
    (ProfileError("application_store_unavailable"), "application_store_unavailable", 503),
    (ProfileError("credential_store_unavailable"), "credential_store_unavailable", 503),
    (CatalogError(CANARY), "service_unavailable", 502),
    (CatalogError("studio_unauthorised"), "service_unavailable", 502),
    (CatalogError("studio_forbidden"), "service_unavailable", 502),
    (CatalogError("method_not_allowed"), "service_unavailable", 502),
    (CatalogError([]), "service_unavailable", 502),
    (ProfileError(CANARY), "service_unavailable", 502),
    (ProfileError("studio_unauthorised"), "service_unavailable", 502),
    (ProfileError(CANARY, CANARY), "metadata_unavailable", 503),
    (RuntimeError(CANARY + KEY), "service_unavailable", 502),
])
@pytest.mark.parametrize("source", ["metadata", "verification", "catalog"])
async def test_typed_and_raw_service_errors_use_fixed_secret_free_envelope(problem, code, status, source,
                                                                         isolation, caplog):
    env = boundary()
    getattr(env, source).error = problem
    if source == "metadata":
        response = await request(env, isolation)
    elif source == "verification":
        response = await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 1})
    else:
        response = await request(env, isolation, suffix=f"/{PROFILE}/projects")
    error(response, code, status)
    assert CANARY not in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize("changes", [
    {"has_token": "true"}, {"has_token": 1}, {"credential_source": CANARY},
    {"has_token": False}, {"revision": True}, {"revision": 0}, {"revision": "1"},
    {"profile_id": CANARY}, {"url": "https://user:" + CANARY + "@catalog.invalid"},
    {"verification": {"state": "unchecked", "token": CANARY}},
    {"verification": {"state": "valid"}},
    {"verification": {**asdict(STATUS), "error_code": CANARY}},
    {"verification": {"state": "checking", "checked_at": CANARY}},
])
async def test_constructed_metadata_is_revalidated_not_coerced_or_echoed(changes, isolation, recwarn):
    env = boundary()
    env.metadata.items = (metadata_item(**changes),)
    error(await request(env, isolation), "invalid_response", 502)
    assert env.verification.calls == env.catalog.calls == env.profiles.calls == []
    # Pydantic serializer diagnostics may contain input_value, including keys.
    # Revalidation must not print those even for a corrupt trusted snapshot.
    assert not recwarn.list


@pytest.mark.parametrize("bad", [None, [], {}, (RemoteServerProfile(profile_id=PROFILE, name="X", url=BASE),)])
async def test_metadata_requires_explicit_complete_tuple_snapshot(bad, isolation):
    env = boundary()
    env.metadata.items = bad
    error(await request(env, isolation), "metadata_unavailable", 503)


async def test_duplicate_metadata_profiles_fail_closed_and_empty_explicit_snapshot_is_valid(isolation):
    env = boundary()
    env.metadata.items *= 2
    error(await request(env, isolation), "invalid_profile_metadata", 502)
    env.metadata.items = ()
    response = await request(env, isolation)
    assert response.status_code == 200 and response.json() == {"items": []}


@pytest.mark.parametrize("model,raw", [
    (VerifyInput, {"expected_revision": 1, "secret": CANARY}),
    (UncheckedVerification, {"state": "unchecked", "checked_at": CANARY}),
    (CheckingVerification, {"state": "checking", "checked_at": CANARY}),
    (VerificationDTO, {**asdict(STATUS), "token": CANARY}),
    (VerificationDTO, {**asdict(STATUS), "wire_protocol": True}),
    (ServerProfile, {**metadata_item().model_dump(), "token": CANARY}),
])
def test_dto_models_are_strict_extra_forbid_and_hide_input(model, raw):
    with pytest.raises(ValidationError) as caught:
        model.model_validate(raw)
    assert CANARY not in str(caught.value)
    assert model.model_config["extra"] == "forbid" and model.model_config["strict"] is True
    assert model.model_config["frozen"] is True


@pytest.mark.parametrize("missing", ["profile_id", "name", "url", "revision", "has_token",
                                     "credential_source", "verification"])
def test_metadata_dto_never_fabricates_missing_snapshot_fields(missing):
    raw = metadata_item().model_dump()
    raw.pop(missing)
    with pytest.raises(ValidationError):
        ServerProfile.model_validate(raw)


@pytest.mark.parametrize("bad", [None, asdict(STATUS), replace(STATUS, wire_protocol=True),
                                 replace(STATUS, state=CANARY), replace(STATUS, error_code=CANARY)])
async def test_verify_service_result_is_typed_and_strict(bad, isolation):
    env = boundary()
    env.verification.result = bad
    error(await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 1}),
          "invalid_response", 502)


@pytest.mark.parametrize("bad", [None, {"items": []}, CatalogPage((replace(PROJECT, metadata_revision=True),), None)])
async def test_catalog_result_is_typed_and_strict(bad, isolation):
    env = boundary()
    env.catalog.result = bad
    error(await request(env, isolation, suffix=f"/{PROFILE}/projects"), "invalid_response", 502)


@pytest.mark.parametrize("changes,code", [({"revision": 2}, "profile_revision_conflict"),
                                        ({"url": OTHER_BASE}, "profile_revision_conflict")])
async def test_router_rechecks_profile_after_injected_catalog_await(changes, code, isolation):
    env = boundary()
    env.catalog.hook = lambda: env.profiles.change(**changes)
    error(await request(env, isolation, suffix=f"/{PROFILE}/projects"), code, 409)
    assert env.catalog.calls == [(PROFILE, 1, None)]


@pytest.mark.parametrize("method,suffix", [("POST", ""), ("PUT", ""), ("DELETE", ""),
                                         ("GET", f"/{PROFILE}/verify"),
                                         ("POST", f"/{PROFILE}/projects"),
                                         ("PATCH", f"/{PROFILE}/projects"),
                                         ("DELETE", f"/{PROFILE}/projects")])
async def test_read_only_route_surface_refuses_mutation_methods(method, suffix, isolation):
    env = boundary()
    response = await request(env, isolation, method, suffix, json={"token": CANARY})
    error(response, "method_not_allowed", 405)
    assert response.headers["Allow"] in {"GET", "POST"}
    idle(env)


@pytest.mark.parametrize("suffix,method", [
    (f"/{PROFILE}", "DELETE"), (f"/{PROFILE}/credentials", "PUT"),
    (f"/{PROFILE}/projects/alpha", "GET"), (f"/{PROFILE}/connect", "POST"),
    (f"/{PROFILE}/select", "POST"), (f"/{PROFILE}/enable", "POST"),
    ("/create", "POST"),
])
async def test_unagreed_mutation_and_detail_routes_are_absent(suffix, method, isolation):
    env = boundary()
    response = await request(env, isolation, method, suffix, routed=False, json={"token": CANARY})
    assert response.status_code == 404
    idle(env)


class Credentials:
    def __init__(self):
        self.key = KEY
        self.calls = []

    def __call__(self, profile_id, url):
        self.calls.append((profile_id, url))
        assert (profile_id, url) == (PROFILE, BASE)
        return self.key


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.hook = None

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": dict(params), "json": json, "follow_redirects": follow_redirects})
        assert method == "GET" and json is None and follow_redirects is False
        assert url.startswith(BASE + "/")
        assert headers["Cache-Control"] == "no-store"
        assert self.replies, "Unexpected transport call, retry or fallback"
        if self.hook:
            await self.hook(len(self.calls))
        await asyncio.sleep(0)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def health(version=2):
    if version == 1:
        return {"status": "ok", "service": "voxarium_studio_remote", "version": "synthetic", "protocol": 1}
    return {"service": "voxarium_studio_remote", "version": "synthetic", "protocol": version,
            "supported_protocols": [version], "catalog_api": version}


def identity(version=2):
    if version == 1:
        return {"protocol": 1, "user": {"id": USER.id, "name": USER.display_name, "color": USER.color}}
    return {"protocol": 2, "catalog_api": 2, "user": asdict(USER),
            "capabilities": ["project_catalog", "explicit_project_create", "strict_connect"]}


def reply(body, status=200):
    return CatalogResponse(status=status, body=deepcopy(body))


def real_boundary(*replies):
    env = boundary()
    env.credentials, env.transport = Credentials(), Transport(*replies)
    env.verification = CatalogVerificationService(profiles=env.profiles, credentials=env.credentials,
                                                 transport=env.transport, clock=lambda: NOW)
    env.catalog = CatalogService(profiles=env.profiles, credentials=env.credentials, transport=env.transport)
    env.app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    env.app.state.auth = AuthSettings()
    env.app.include_router(create_remote_router(metadata=env.metadata, profiles=env.profiles,
                                               verification=env.verification, catalog=env.catalog))
    return env


@pytest.mark.parametrize("version", [1, 2])
async def test_real_verify_reports_v1_valid_but_catalog_disabled(version, isolation):
    env = real_boundary(reply(health(version)), reply(identity(version)))
    response = await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 1})
    expected = replace(STATUS, wire_protocol=version, catalog_version=2 if version == 2 else None,
                       error_code=None if version == 2 else "catalog_unavailable")
    assert response.status_code == 200 and response.json() == asdict(expected)
    assert [c["url"] for c in env.transport.calls] == [BASE + "/healthz", BASE + f"/api/v{version}/me"]
    assert "Authorization" not in env.transport.calls[0]["headers"]
    assert env.transport.calls[1]["headers"]["Authorization"] == "Bearer " + KEY
    assert set(env.credentials.calls) == {(PROFILE, BASE)}
    assert env.metadata.calls == [] and env.profiles.items[PROFILE].revision == 1


@pytest.mark.parametrize("version", [1, 3])
async def test_real_projects_reject_old_or_unsupported_protocol_without_fallback(version, isolation):
    env = real_boundary(reply(health(version)))
    error(await request(env, isolation, suffix=f"/{PROFILE}/projects"), "version_mismatch", 502)
    assert len(env.transport.calls) == 1
    assert "Authorization" not in env.transport.calls[0]["headers"]


async def test_real_catalog_translates_cursor_and_does_not_reuse_verify_as_permission(isolation):
    page = {"protocol": 2, "projects": [asdict(PROJECT)], "next_after": PROJECT.id}
    env = real_boundary(reply(health(1)), reply(identity(1)),
                        reply(health()), reply(identity()), reply(page))
    assert (await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 1})).status_code == 200
    response = await request(env, isolation, suffix=f"/{PROFILE}/projects", params={"cursor": "a"})
    assert response.status_code == 200
    assert response.json() == {"items": [asdict(PROJECT)], "next_cursor": "alpha"}
    assert [c["url"] for c in env.transport.calls] == [BASE + suffix for suffix in
        ("/healthz", "/api/v1/me", "/healthz", "/api/v2/me", "/api/v2/projects")]
    assert env.transport.calls[-1]["params"] == {"limit": 100, "after": "a"}
    assert env.credentials.key == KEY and env.profiles.items[PROFILE].revision == 1


@pytest.mark.parametrize("surface", ["verify-user-v1", "verify-user-v2", "health", "project", "timestamp"])
async def test_real_services_reject_remote_success_echoing_token(surface, isolation, caplog):
    version = 1 if surface.endswith("v1") else 2
    bodies = [health(version), identity(version)]
    if surface == "health":
        bodies[0]["version"] = KEY
    elif surface.startswith("verify"):
        bodies[1]["user"]["name" if version == 1 else "display_name"] = "prefix " + KEY
    else:
        project = asdict(PROJECT)
        project["display_name" if surface == "project" else "created_at"] = KEY
        bodies.append({"protocol": 2, "projects": [project], "next_after": None})
    env = real_boundary(*(reply(body) for body in bodies))
    is_verify = surface.startswith("verify")
    response = await request(env, isolation, "POST" if is_verify else "GET",
                             f"/{PROFILE}/verify" if is_verify else f"/{PROFILE}/projects",
                             **({"json": {"expected_revision": 1}} if is_verify else {}))
    error(response, "invalid_response", 502)
    assert len(env.transport.calls) == (1 if surface == "health" else len(bodies))
    assert KEY not in caplog.text


@pytest.mark.parametrize("action,stage", [("verify", 1), ("verify", 2),
                                         ("projects", 1), ("projects", 2), ("projects", 3)])
@pytest.mark.parametrize("mutation,code,status", [
    ("revision", "profile_revision_conflict", 409), ("url", "profile_revision_conflict", 409),
    ("key", "credential_binding_conflict", 409), ("delete", "profile_not_found", 404),
])
async def test_real_service_fences_profile_revision_and_key_at_each_await(action, stage, mutation, code,
                                                                      status, isolation):
    script = [reply(health()), reply(identity())]
    if action == "projects":
        script.append(reply({"protocol": 2, "projects": [asdict(PROJECT)], "next_after": None}))
    env = real_boundary(*script)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    env.transport.hook = pause
    kwargs = {"json": {"expected_revision": 1}} if action == "verify" else {}
    operation = asyncio.create_task(request(env, isolation, "POST" if action == "verify" else "GET",
                                             f"/{PROFILE}/{action}", **kwargs))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "revision":
            env.profiles.change(revision=2)
        elif mutation == "url":
            env.profiles.change(url=OTHER_BASE)
        elif mutation == "key":
            env.credentials.key = "SYNTHETIC-ROTATED-KEY-119"
        else:
            del env.profiles.items[PROFILE]
        resume.set()
        error(await asyncio.wait_for(operation, timeout=2), code, status)
        assert len(env.transport.calls) == stage
        assert set(env.credentials.calls) == {(PROFILE, BASE)}
    finally:
        resume.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_real_verify_stale_revision_stops_before_key_and_transport(isolation):
    env = real_boundary()
    error(await request(env, isolation, "POST", f"/{PROFILE}/verify", json={"expected_revision": 2}),
          "profile_revision_conflict", 409)
    assert env.credentials.calls == env.transport.calls == []


@pytest.mark.parametrize("action", ["verify", "projects"])
async def test_real_services_scrub_raw_transport_exception(action, isolation, caplog):
    env = real_boundary(RuntimeError(KEY + CANARY))
    kwargs = {"json": {"expected_revision": 1}} if action == "verify" else {}
    response = await request(env, isolation, "POST" if action == "verify" else "GET", f"/{PROFILE}/{action}", **kwargs)
    if action == "verify":
        assert response.status_code == 200
        assert response.json() == asdict(VerificationStatus("offline", STAMP, error_code="transport_unavailable"))
    else:
        error(response, "transport_unavailable", 502)
    assert len(env.transport.calls) == 1
    assert KEY not in caplog.text and CANARY not in caplog.text

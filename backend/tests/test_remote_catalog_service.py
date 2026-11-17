"""Synthetic profile-bound reads (remote-identity/studio-design §5.1/§6).

Only in-memory providers and an explicitly scripted transport are permitted.
No HTTP adapter, real credential store, managed project or server is involved.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import inspect
from pathlib import Path
import socket
import traceback
from types import SimpleNamespace

import pytest

from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.sync.catalog import (
    CatalogError, CatalogPage, CatalogProject, CatalogResponse, Verification,
)
from voxarium_studio.sync.catalog_service import CatalogService
from voxarium_studio.sync.credentials import CredentialsError
from voxarium_studio.sync.profiles import ProfileError


PROFILE_A = "00000000-0000-4000-8000-000000000110"
PROFILE_B = "00000000-0000-4000-8000-000000000111"
BASE_A = "https://catalog.invalid/deploy/alpha"
BASE_B = "https://catalog.invalid/deploy/beta"
KEY_A = "SYNTHETIC-SERVICE-KEY-CANARY-A-110"
KEY_B = "SYNTHETIC-SERVICE-KEY-CANARY-B-110"
EPOCH = "0123456789abcdef0123456789abcdef"
CAPABILITIES = ["project_catalog", "explicit_project_create", "strict_connect"]


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """Single-threaded tests need no wakeup socket (Windows socketpair listens).

    Only ready callbacks, Events and timers are used here. This intentionally
    does not support cross-thread wakeups or real network operations.
    """

    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("Synthetic tests must not schedule cross-thread I/O")

    def _loop_self_reading(self, *_args):
        pass


@pytest.fixture(scope="module")
def event_loop_policy():
    class SyntheticPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return SocketFreeLoop()

    return SyntheticPolicy()


def health(**changes):
    return {"service": "voxarium_studio_remote", "version": "synthetic-v2",
            "protocol": 2, "supported_protocols": [2], "catalog_api": 2, **changes}


def identity(**changes):
    return {"protocol": 2, "catalog_api": 2, "capabilities": CAPABILITIES.copy(),
            "user": {"id": "user-a", "display_name": "Synthetic user", "color": "#3e63dd"},
            **changes}


def project(id="alpha", **changes):
    return {"id": id, "display_name": "Same display name", "storage_epoch": EPOCH,
            "metadata_revision": 1, "created_at": "2026-11-15T12:00:00Z", **changes}


def page(*ids, **changes):
    return {"protocol": 2, "projects": [project(id) for id in ids], "next_after": None,
            **changes}


def response(body, status=200, **kwargs):
    return CatalogResponse(status=status, body=deepcopy(body), **kwargs)


class Profiles:
    def __init__(self):
        self.items = {
            PROFILE_A: RemoteServerProfile(profile_id=PROFILE_A, name="Alpha", url=BASE_A),
            PROFILE_B: RemoteServerProfile(profile_id=PROFILE_B, name="Beta", url=BASE_B),
        }
        self.calls = []
        self.error = None

    def get(self, profile_id):
        self.calls.append(profile_id)
        if self.error is not None:
            raise self.error
        if profile_id not in self.items:
            raise ProfileError("profile_not_found")
        return self.items[profile_id]

    def change(self, **changes):
        # model_copy deliberately allows corrupt disk/provider metadata in tests.
        self.items[PROFILE_A] = self.items[PROFILE_A].model_copy(update=changes)


class Credentials:
    def __init__(self):
        self.keys = {(PROFILE_A, BASE_A): KEY_A, (PROFILE_B, BASE_B): KEY_B}
        self.calls = []
        self.error = None
        self.hook = None

    def __call__(self, profile_id, url):
        self.calls.append((profile_id, url))
        if self.error is not None:
            raise self.error
        if self.hook is not None:
            self.hook()
        return self.keys.get((profile_id, url))


class Transport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.hook = None

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": deepcopy(params), "json": deepcopy(json),
                           "follow_redirects": follow_redirects})
        assert follow_redirects is False
        assert self.replies, "Unexpected transport call or blind retry"
        if self.hook is not None:
            await self.hook(len(self.calls))
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def build(*replies):
    profiles, credentials, transport = Profiles(), Credentials(), Transport(*replies)
    service = CatalogService(profiles=profiles, credentials=credentials, transport=transport)
    return service, profiles, credentials, transport


async def invoke(service, action, profile_id=PROFILE_A, revision=1):
    if action == "verify":
        return await service.verify(profile_id, expected_revision=revision)
    if action == "list":
        return await service.list_projects(profile_id, expected_revision=revision)
    return await service.get_project(profile_id, "alpha", expected_revision=revision)


def successful_replies(action):
    replies = [response(health()), response(identity())]
    if action == "list":
        replies.append(response(page("alpha")))
    elif action == "get":
        replies.append(response({"protocol": 2, "project": project()}))
    return replies


def assert_secret_free(error, caplog=None):
    diagnostic = (str(error), repr(error), error.args, error.__dict__,
                  "".join(traceback.format_exception(error)))
    for key in (KEY_A, KEY_B):
        assert key not in repr(diagnostic)
        if caplog is not None:
            assert key not in caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    """Even accidental fallback must not reach DNS, sockets or personal stores."""
    from voxarium_studio.core import application_config
    from voxarium_studio.sync import credentials, profiles

    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic catalog service attempted external I/O")

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    for name in ("_load_application_config_strict", "_write_application_profiles",
                 "application_config_transaction"):
        monkeypatch.setattr(application_config, name, forbidden)
    monkeypatch.setattr(profiles.RemoteProfiles, "get", forbidden)
    for name in ("load_profile_token", "save_profile_token", "load_token", "save_token",
                 "import_legacy_token"):
        monkeypatch.setattr(credentials, name, forbidden)
    for name in ("VOXARIUM_STUDIO_REMOTE_URL", "VOXARIUM_STUDIO_REMOTE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


async def test_two_profiles_keep_exact_prefix_and_key_binding():
    service, profiles, credentials, transport = build(
        *successful_replies("list"), *successful_replies("list"),
        *successful_replies("get"),
    )
    first = await service.list_projects(PROFILE_A, expected_revision=1, limit=7, after="alpha")
    second = await service.list_projects(PROFILE_B, expected_revision=1)
    exact = await service.get_project(PROFILE_A, "alpha", expected_revision=1)
    assert isinstance(first, CatalogPage) and isinstance(second, CatalogPage)
    assert isinstance(exact, CatalogProject)
    assert first.projects[0].id == second.projects[0].id == exact.id == "alpha"
    assert isinstance(first.projects, tuple)
    assert exact.space_instance_id == EPOCH
    assert [(c["method"], c["url"]) for c in transport.calls] == [
        ("GET", base + suffix)
        for base, tail in ((BASE_A, "/api/v2/projects"), (BASE_B, "/api/v2/projects"),
                           (BASE_A, "/api/v2/projects/alpha"))
        for suffix in ("/healthz", "/api/v2/me", tail)
    ]
    assert transport.calls[2]["params"] == {"limit": 7, "after": "alpha"}
    assert transport.calls[5]["params"] == {"limit": 100}
    assert set(credentials.calls) == {(PROFILE_A, BASE_A), (PROFILE_B, BASE_B)}
    assert set(profiles.calls) == {PROFILE_A, PROFILE_B}
    for index, call in enumerate(transport.calls):
        key = KEY_B if 3 <= index < 6 else KEY_A
        assert call["json"] is None and call["follow_redirects"] is False
        assert call["headers"]["Cache-Control"] == "no-store"
        assert "Cookie" not in call["headers"]
        if index % 3 == 0:
            assert "Authorization" not in call["headers"]
        else:
            assert call["headers"]["Authorization"] == "Bearer " + key
        for secret in (KEY_A, KEY_B):
            assert secret not in repr((call["url"], call["params"], call["json"]))
    assert KEY_A not in repr((service, first, second, exact))
    assert KEY_B not in repr((service, first, second, exact))


async def test_every_read_reverifies_and_returns_immutable_dtos():
    service, profiles, credentials, transport = build(
        *successful_replies("verify"), *successful_replies("verify"),
        *successful_replies("list"), *successful_replies("get"),
    )
    for action in ("verify", "verify", "list", "get"):
        result = await invoke(service, action)
        if action == "verify":
            assert isinstance(result, Verification)
            assert result.capabilities == frozenset(CAPABILITIES)
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            result.synthetic_mutation = True
        assert KEY_A not in repr(asdict(result))
    assert sum(c["url"].endswith("/healthz") for c in transport.calls) == 4
    assert sum(c["url"].endswith("/api/v2/me") for c in transport.calls) == 4
    assert all(c["method"] == "GET" and c["json"] is None for c in transport.calls)
    assert all(p.revision == 1 for p in profiles.items.values())
    assert credentials.keys == {(PROFILE_A, BASE_A): KEY_A, (PROFILE_B, BASE_B): KEY_B}


async def test_overlapping_profiles_do_not_rebind_waiting_request():
    service, _, _, transport = build(*successful_replies("list"), *successful_replies("list"))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == 1:
            entered.set()
            await resume.wait()

    transport.hook = pause
    first = asyncio.create_task(invoke(service, "list"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await invoke(service, "list", profile_id=PROFILE_B)
        resume.set()
        result = await asyncio.wait_for(first, timeout=2)
        assert result.projects[0].id == second.projects[0].id == "alpha"
        assert len(transport.calls) == 6
        for call in transport.calls:
            if call["url"].endswith("/healthz"):
                assert "Authorization" not in call["headers"]
            else:
                key = KEY_A if call["url"].startswith(BASE_A + "/") else KEY_B
                assert call["headers"]["Authorization"] == "Bearer " + key
    finally:
        resume.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("action", ["verify", "list", "get"])
@pytest.mark.parametrize("revision", [0, 2, True, "1", None])
async def test_stale_revision_rejected_before_credentials_and_transport(action, revision):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action, revision=revision)
    assert caught.value.code == "profile_revision_conflict"
    assert credentials.calls == [] and transport.calls == []
    assert_secret_free(caught.value)


@pytest.mark.parametrize("action,stage", [
    ("verify", 1), ("verify", 2), ("list", 1), ("list", 2), ("list", 3),
    ("get", 1), ("get", 2), ("get", 3),
])
@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"),
    ("url", "profile_revision_conflict"),
    ("key", "credential_binding_conflict"),
    ("delete", "profile_not_found"),
])
async def test_inflight_snapshot_changes_stop_at_each_await(action, stage, mutation, code):
    service, profiles, credentials, transport = build(*successful_replies(action))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    operation = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "revision":
            profiles.change(revision=2)
        elif mutation == "url":
            profiles.change(url=BASE_B)
        elif mutation == "key":
            credentials.keys[(PROFILE_A, BASE_A)] = KEY_B
        else:
            del profiles.items[PROFILE_A]
        resume.set()
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(operation, timeout=2)
        assert caught.value.code == code
        assert len(transport.calls) == stage
        assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
        assert_secret_free(caught.value)
    finally:
        resume.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_revision_rechecked_after_credential_provider_before_transport():
    service, profiles, credentials, transport = build()
    credentials.hook = lambda: profiles.change(revision=2)
    with pytest.raises(CatalogError) as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert caught.value.code == "profile_revision_conflict"
    assert credentials.calls and transport.calls == []


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_failed_transport_await_still_checks_snapshot(stage):
    replies = successful_replies("list")
    replies[stage - 1] = RuntimeError(KEY_A)
    service, profiles, _, transport = build(*replies)

    async def change(index):
        if index == stage:
            profiles.change(revision=2)
        await asyncio.sleep(0)

    transport.hook = change
    with pytest.raises(CatalogError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert caught.value.code == "profile_revision_conflict"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value)


@pytest.mark.parametrize("source,problem,code", [
    ("profiles", ProfileError("profile_not_found"), "profile_not_found"),
    ("profiles", ProfileError("application_store_unavailable"), "profile_store_unavailable"),
    ("profiles", OSError(KEY_A), "profile_store_unavailable"),
    ("credentials", CredentialsError("credential_store_corrupt"), "credential_store_unavailable"),
    ("credentials", RuntimeError(KEY_A), "credential_store_unavailable"),
])
async def test_provider_errors_are_fixed_and_secret_free(source, problem, code, caplog):
    service, profiles, credentials, transport = build()
    (profiles if source == "profiles" else credentials).error = problem
    with pytest.raises(CatalogError) as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert caught.value.code == code
    assert transport.calls == []
    if source == "profiles":
        assert credentials.calls == []
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("token", [None])
async def test_missing_key_fails_closed_without_transport(token):
    service, _, credentials, transport = build()
    credentials.keys[(PROFILE_A, BASE_A)] = token
    with pytest.raises(CatalogError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert caught.value.code == "credential_required"
    assert transport.calls == []


@pytest.mark.parametrize("token", ["", " ", "bad\nkey", 110])
async def test_malformed_key_fails_closed_without_transport(token):
    service, _, credentials, transport = build()
    credentials.keys[(PROFILE_A, BASE_A)] = token
    with pytest.raises(CatalogError) as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert caught.value.code == "invalid_credential"
    assert transport.calls == []


@pytest.mark.parametrize("changes", [
    {"profile_id": PROFILE_B}, {"profile_id": "not-a-profile-id"},
    {"revision": True}, {"revision": 0}, {"revision": "1"},
    {"url": "https://user:secret@catalog.invalid"}, {"url": None},
])
async def test_corrupt_profile_metadata_never_reaches_key_provider(changes):
    service, profiles, credentials, transport = build()
    profiles.change(**changes)
    with pytest.raises(CatalogError) as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert caught.value.code == "invalid_profile_metadata"
    assert credentials.calls == [] and transport.calls == []
    assert_secret_free(caught.value)


@pytest.mark.parametrize("raw", ["https://CATALOG.INVALID/deploy/alpha/",
                                  "wss://catalog.invalid/deploy/alpha/"])
async def test_provider_and_transport_share_normalized_profile_url(raw):
    service, profiles, credentials, transport = build(*successful_replies("verify"))
    profiles.items[PROFILE_A] = RemoteServerProfile(profile_id=PROFILE_A, name="Alpha", url=raw)
    await service.verify(PROFILE_A, expected_revision=1)
    assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
    assert [c["url"] for c in transport.calls] == [BASE_A + "/healthz", BASE_A + "/api/v2/me"]


@pytest.mark.parametrize("action", ["verify", "list", "get"])
@pytest.mark.parametrize("bad_reply,expected_calls,code", [
    (response(health(protocol=1)), 1, "version_mismatch"),
    (response({"protocol": 2}), 1, "invalid_response"),
    (response({}, status=302, headers={"Location": "https://other.invalid/"}), 1, "redirect_refused"),
    (response({"error": {"code": "unauthorised", "message": KEY_A, "request_id": KEY_A}},
              status=401), 2, "unauthorised"),
    (response(identity(capabilities=[])), 2, "capabilities_missing"),
    (response(identity(protocol=3)), 2, "version_mismatch"),
])
async def test_remote_failures_never_fall_back_or_continue(action, bad_reply, expected_calls, code, caplog):
    replies = ([response(health())] if expected_calls == 2 else []) + [bad_reply]
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == code
    assert len(transport.calls) == expected_calls
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action,stage", [("verify", 1), ("verify", 2), ("list", 3), ("get", 3)])
async def test_transport_exception_canary_is_not_retained(action, stage, caplog):
    replies = successful_replies(action)
    replies[stage - 1] = RuntimeError(KEY_A)
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == "transport_unavailable"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_cancellation_canary_is_scrubbed_and_never_retried(stage, caplog):
    replies = successful_replies("list")
    replies[stage - 1] = asyncio.CancelledError(KEY_A)
    service, _, _, transport = build(*replies)
    with pytest.raises(asyncio.CancelledError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("stage", [2, 3])
async def test_authenticated_redirect_is_not_followed(stage):
    replies = successful_replies("list")
    replies[stage - 1] = response({}, status=307, headers={"Location": BASE_B})
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert caught.value.code == "redirect_refused"
    assert len(transport.calls) == stage
    assert all(call["url"].startswith(BASE_A + "/") for call in transport.calls)


@pytest.mark.parametrize("bad_reply,code", [
    (response({"error": {"code": "unauthorised", "message": KEY_A, "request_id": KEY_A}},
              status=401), "unauthorised"),
    (response(page("alpha", protocol=1)), "version_mismatch"),
    (response(page("beta", "alpha")), "invalid_response"),
    (response(page("alpha", next_after="beta")), "invalid_response"),
])
async def test_catalog_response_failures_return_no_partial_page(bad_reply, code, caplog):
    service, _, _, transport = build(response(health()), response(identity()), bad_reply)
    with pytest.raises(CatalogError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert caught.value.code == code
    assert len(transport.calls) == 3
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("surface", ["user", "capability", "project", "created_at"])
async def test_successful_response_echoing_credential_is_rejected(surface, caplog):
    replies = successful_replies("get")
    if surface == "user":
        replies[1].body["user"]["display_name"] = "prefix " + KEY_A + " suffix"
    elif surface == "capability":
        replies[1].body["capabilities"].append(KEY_A)
    else:
        field = "display_name" if surface == "project" else "created_at"
        replies[2].body["project"][field] = KEY_A
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await service.get_project(PROFILE_A, "alpha", expected_revision=1)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == {"user": 2, "capability": 2,
                                    "project": 3, "created_at": 3}[surface]


async def test_successful_page_echoing_key_in_historical_label_is_rejected(caplog):
    body = page("alpha", "beta")
    body["projects"][1]["display_name"] = KEY_A
    service, _, _, transport = build(response(health()), response(identity()), response(body))
    with pytest.raises(CatalogError) as caught:
        await service.list_projects(PROFILE_A, expected_revision=1)
    assert caught.value.code == "invalid_response"
    assert len(transport.calls) == 3
    assert_secret_free(caught.value, caplog)


async def test_failure_does_not_cache_verification_or_use_other_profile_key():
    service, _, credentials, transport = build(
        *successful_replies("verify"),
        response(health()), response({"error": {"code": "unauthorised", "message": KEY_A,
                                               "request_id": KEY_A}}, status=401),
        *successful_replies("get"),
    )
    await service.verify(PROFILE_A, expected_revision=1)
    with pytest.raises(CatalogError):
        await service.list_projects(PROFILE_A, expected_revision=1)
    await service.get_project(PROFILE_A, "alpha", expected_revision=1)
    assert [c["url"] for c in transport.calls] == [
        BASE_A + suffix for suffix in ("/healthz", "/api/v2/me", "/healthz", "/api/v2/me",
                                      "/healthz", "/api/v2/me", "/api/v2/projects/alpha")]
    assert all(key == (PROFILE_A, BASE_A) for key in credentials.calls)
    assert KEY_B not in repr(transport.calls)


def test_no_defaults_arbitrary_url_or_mutation_entrypoints():
    constructor = inspect.signature(CatalogService)
    assert set(constructor.parameters) == {"profiles", "credentials", "transport"}
    assert all(p.default is inspect.Parameter.empty for p in constructor.parameters.values())
    service, _, _, _ = build()
    for name in ("verify", "list_projects", "get_project"):
        signature = inspect.signature(getattr(service, name))
        assert signature.parameters["profile_id"].default is inspect.Parameter.empty
        revision = signature.parameters["expected_revision"]
        assert revision.kind is inspect.Parameter.KEYWORD_ONLY
        assert revision.default is inspect.Parameter.empty
        assert not {"url", "token", "headers"} & set(signature.parameters)
    for name in ("create_project", "select_project", "connect", "enable", "activate", "save"):
        assert not hasattr(service, name)
    with pytest.raises(TypeError):
        CatalogService(profiles=Profiles(), credentials=Credentials())


async def test_arbitrary_url_is_never_treated_as_profile_or_credential_namespace():
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError) as caught:
        await service.verify("https://arbitrary.invalid/prefix", expected_revision=1)
    assert caught.value.code in {"profile_not_found", "invalid_profile_metadata"}
    assert credentials.calls == [] and transport.calls == []


async def test_repeated_reads_leave_synthetic_files_untouched(tmp_path, monkeypatch):
    """Guard writes themselves, not just a final content comparison."""
    import builtins
    import io
    import os

    fixture = tmp_path / "synthetic-config.txt"
    fixture.write_text("synthetic, not a real config", encoding="utf-8")
    before = fixture.read_bytes()
    before_names = sorted(p.name for p in tmp_path.iterdir())
    service, _, _, transport = build(
        *successful_replies("verify"), *successful_replies("verify"),
        *successful_replies("list"), *successful_replies("get"),
    )

    def forbidden(*_args, **_kwargs):
        pytest.fail("Read-only service attempted a filesystem mutation")

    builtin_open, io_open, os_open = builtins.open, io.open, os.open

    def guard_open(original):
        def opened(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return original(file, mode, *args, **kwargs)
        return opened

    def guard_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard_open(builtin_open))
        patch.setattr(io, "open", guard_open(io_open))
        patch.setattr(os, "open", guard_os_open)
        for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "touch"):
            patch.setattr(Path, name, forbidden)
        for action in ("verify", "verify", "list", "get"):
            await invoke(service, action)
    assert fixture.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == before_names
    assert len(transport.calls) == 10


async def test_health_echo_is_rejected_before_authentication(caplog):
    service, _, _, transport = build(response(health(version=KEY_A)))
    with pytest.raises(CatalogError) as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert caught.value.code == "invalid_response"
    assert len(transport.calls) == 1
    assert "Authorization" not in transport.calls[0]["headers"]
    assert_secret_free(caught.value, caplog)

"""HTTP-only Verify fixtures (studio-design §5.1/§6; offline gaps slice D).

No Remote package, real transport, configuration, credential store or runtime.
Run with --basetemp under the task's disposable directory and -n0 (no IPC).
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from datetime import datetime, timedelta, timezone
import inspect
import socket
import traceback
from types import SimpleNamespace

import pytest

from voxarium_studio.core.application_config import RemoteServerProfile
from voxarium_studio.sync.catalog import CatalogClient, CatalogError, CatalogResponse, CatalogUser
from voxarium_studio.sync.catalog_verification import CatalogVerificationService, VerificationStatus
from voxarium_studio.sync.credentials import CredentialsError
from voxarium_studio.sync.profiles import ProfileError


PROFILE = "00000000-0000-4000-8000-000000000117"
OTHER_PROFILE = "00000000-0000-4000-8000-000000000118"
BASE = "https://verify.invalid/deployment/prefix"
OTHER_BASE = "https://other.invalid/private"
KEY = "SYNTHETIC-VERIFY-SECRET-CANARY-117"
OTHER_KEY = "SYNTHETIC-VERIFY-SECRET-CANARY-118"
NOW = datetime(2026, 11, 15, 15, 30, 45, 123456, tzinfo=timezone(timedelta(hours=3)))
CAPABILITIES = ["project_catalog", "explicit_project_create", "strict_connect"]


class SocketFreeLoop(getattr(asyncio, "ProactorEventLoop", asyncio.SelectorEventLoop)):
    """As in test_remote_catalog_service: no Windows socketpair/listener."""

    def _make_self_pipe(self):
        self._ssock = None
        self._csock = SimpleNamespace(fileno=lambda: -1)

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        raise AssertionError("Synthetic Verify must not schedule cross-thread I/O")

    def _loop_self_reading(self, *_args):
        pass


@pytest.fixture(scope="module")
def event_loop_policy():
    class SyntheticPolicy(asyncio.DefaultEventLoopPolicy):
        def new_event_loop(self):
            return SocketFreeLoop()

    return SyntheticPolicy()


@pytest.fixture(autouse=True)
def synthetic_only(monkeypatch):
    from voxarium_studio.core import application_config
    from voxarium_studio.sync import credentials, profiles

    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic Verify attempted external I/O or catalog/mutation access")

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
    for name in ("get_project", "list_projects", "create_project"):
        monkeypatch.setattr(CatalogClient, name, forbidden)
    for name in ("VOXARIUM_STUDIO_REMOTE_URL", "VOXARIUM_STUDIO_REMOTE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def health(version=1, **changes):
    body = {"status": "ok", "service": "voxarium_studio_remote",
            "version": "synthetic-release", "protocol": version}
    if version == 2:
        body.update(supported_protocols=[2], catalog_api=2)
    return {**body, **changes}


def identity(version=1, **changes):
    user = {"id": "synthetic-user", "color": "#A1b2C3"}
    user["name" if version == 1 else "display_name"] = "Synthetic user"
    body = {"protocol": version, "user": user}
    if version == 2:
        body.update(catalog_api=2, capabilities=CAPABILITIES.copy())
    return {**body, **changes}


def response(body, status=200, **kwargs):
    return CatalogResponse(status=status, body=deepcopy(body), **kwargs)


def replies(version=1):
    return [response(health(version)), response(identity(version))]


class Profiles:
    def __init__(self):
        self.items = {PROFILE: RemoteServerProfile(profile_id=PROFILE, name="Verify", url=BASE)}
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
        self.items[PROFILE] = self.items[PROFILE].model_copy(update=changes)


class Credentials:
    def __init__(self):
        self.keys = {(PROFILE, BASE): KEY}
        self.calls = []
        self.error = None
        self.hook = None

    def __call__(self, profile_id, url):
        self.calls.append((profile_id, url))
        if self.error is not None:
            raise self.error
        if self.hook:
            self.hook()
        return self.keys.get((profile_id, url))


class Transport:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.hook = None

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": deepcopy(params), "json": deepcopy(json),
                           "follow_redirects": follow_redirects})
        assert method == "GET" and json is None and follow_redirects is False
        assert params == {}
        assert url in {BASE + path for path in ("/healthz", "/api/v1/me", "/api/v2/me")}
        assert self.script, "Unexpected request, guessed route or retry"
        if self.hook:
            await self.hook(len(self.calls))
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def build(*script, clock=lambda: NOW):
    profiles, credentials, transport = Profiles(), Credentials(), Transport(*script)
    service = CatalogVerificationService(profiles=profiles, credentials=credentials,
                                         transport=transport, clock=clock)
    return service, profiles, credentials, transport


async def verify(service, revision=1):
    return await service.verify(PROFILE, expected_revision=revision)


def assert_secret_free(value, caplog):
    diagnostic = repr(value)
    if isinstance(value, BaseException):
        diagnostic += repr((value.args, value.__dict__, traceback.format_exception(value)))
        assert value.__cause__ is None and value.__context__ is None
    for secret in (KEY, OTHER_KEY):
        assert secret not in diagnostic
        assert secret not in caplog.text


def assert_status(status, state, code):
    assert isinstance(status, VerificationStatus)
    assert status.state == state and status.error_code == code
    checked = datetime.fromisoformat(status.checked_at.replace("Z", "+00:00"))
    assert checked == NOW and checked.utcoffset() == timedelta(0)


@pytest.mark.parametrize("version", [1, 2])
async def test_exact_http_only_verify_and_frozen_secret_free_status(version, caplog):
    service, profiles, credentials, transport = build(*replies(version))
    status = await verify(service)
    assert_status(status, "valid", "catalog_unavailable" if version == 1 else None)
    assert status.user == CatalogUser("synthetic-user", "Synthetic user", "#A1b2C3")
    assert status.wire_protocol == version
    assert status.catalog_version == (2 if version == 2 else None)
    assert set(asdict(status)) == {"state", "checked_at", "user", "wire_protocol",
                                    "catalog_version", "error_code"}
    for dto in (status, status.user):
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            dto.synthetic_mutation = True
    assert [call["url"] for call in transport.calls] == [BASE + "/healthz", BASE + f"/api/v{version}/me"]
    assert "Authorization" not in transport.calls[0]["headers"]
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer " + KEY
    assert all(call["headers"]["Cache-Control"] == "no-store" for call in transport.calls)
    assert set(credentials.calls) == {(PROFILE, BASE)}
    assert profiles.items[PROFILE].revision == 1
    assert credentials.keys == {(PROFILE, BASE): KEY}
    assert_secret_free((service, status, asdict(status)), caplog)


@pytest.mark.parametrize("version", [1, 2])
async def test_401_is_remote_unauthorised_not_fallback_or_catalog_permission(version, caplog):
    # v1 FastAPI uses detail, not the future v2 error envelope.
    body = {"detail": KEY} if version == 1 else {
        "error": {"code": "unauthorised", "message": KEY, "request_id": KEY}}
    service, _, _, transport = build(response(health(version)), response(body, 401))
    status = await verify(service)
    assert_status(status, "unauthorised", "remote_unauthorised")
    assert status.user is None and status.catalog_version is None
    assert len(transport.calls) == 2
    assert_secret_free(status, caplog)


@pytest.mark.parametrize("bad_health,code", [
    (health(3), "version_mismatch"),
    (health(0), "version_mismatch"),
    (health(protocol=True), "invalid_response"),
    (health(protocol="1"), "invalid_response"),
    ({}, "invalid_response"),
    (None, "invalid_response"),
    ({"protocol": 1}, "invalid_response"),
    ({k: v for k, v in health().items() if k != "status"}, "invalid_response"),
    (health(status="degraded"), "invalid_response"),
    (health(service="other_service"), "invalid_response"),
    (health(version=1, extra="not agreed"), "invalid_response"),
    (health(version=1, supported_protocols=[2], catalog_api=2), "invalid_response"),
    ({**health(), "version": 1}, "invalid_response"),
])
async def test_unknown_or_malformed_discovery_never_sends_authorization(bad_health, code, caplog):
    service, _, _, transport = build(response(bad_health))
    status = await verify(service)
    assert_status(status, "incompatible", code)
    assert status.user is None and status.catalog_version is None
    assert len(transport.calls) == 1
    assert "Authorization" not in transport.calls[0]["headers"]
    assert_secret_free(status, caplog)


@pytest.mark.parametrize("body,code", [
    ({"protocol": 2, "user": identity()["user"]}, "version_mismatch"),
    (identity(protocol=True), "version_mismatch"),
    (identity(protocol="1"), "version_mismatch"),
    ({"user": identity()["user"]}, "invalid_response"),
    (identity(extra="forbidden"), "invalid_response"),
    (identity(user={"id": "u", "display_name": "Not v1", "color": "#123456"}), "invalid_response"),
    (identity(user={"id": "u", "name": "User", "color": "#123456", "extra": 1}), "invalid_response"),
    (identity(user={"id": "", "name": "User", "color": "#123456"}), "invalid_response"),
    (identity(user={"id": 1, "name": "User", "color": "#123456"}), "invalid_response"),
    (identity(user={"id": "u", "name": None, "color": "#123456"}), "invalid_response"),
    (identity(user={"id": "u", "name": "User", "color": "red"}), "invalid_response"),
])
async def test_v1_identity_envelope_is_exact_not_a_v2_or_guessed_fallback(body, code):
    service, _, _, transport = build(response(health()), response(body))
    status = await verify(service)
    assert_status(status, "incompatible", code)
    assert status.user is None
    assert [call["url"] for call in transport.calls] == [BASE + "/healthz", BASE + "/api/v1/me"]


@pytest.mark.parametrize("stage,body,code", [
    (1, health(2, catalog_api=1), "version_mismatch"),
    (1, health(2, supported_protocols=[True]), "version_mismatch"),
    (1, health(2, extra=1), "invalid_response"),
    (2, identity(2, capabilities=[]), "capabilities_missing"),
    (2, identity(2, catalog_api=1), "version_mismatch"),
    (2, identity(2, protocol=1), "version_mismatch"),
    (2, identity(2, extra=1), "invalid_response"),
    (2, identity(2, user=identity()["user"]), "invalid_response"),
])
async def test_v2_keeps_strict_catalog_client_gate_and_never_downgrades(stage, body, code):
    script = replies(2)
    script[stage - 1] = response(body)
    service, _, _, transport = build(*script)
    status = await verify(service)
    assert_status(status, "incompatible", code)
    assert status.user is None and status.catalog_version is None
    assert len(transport.calls) == stage
    assert not any("/v1/" in call["url"] for call in transport.calls)


@pytest.mark.parametrize("revision", [0, 2, True, "1", None])
async def test_stale_revision_precedes_credentials_and_transport(revision, caplog):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError) as caught:
        await verify(service, revision)
    assert caught.value.code == "profile_revision_conflict"
    assert credentials.calls == transport.calls == []
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"), ("url", "profile_revision_conflict"),
    ("key", "credential_binding_conflict"), ("delete", "profile_not_found"),
])
@pytest.mark.parametrize("version,stage", [(1, 1), (1, 2), (2, 1), (2, 2)])
@pytest.mark.parametrize("failed", [False, True])
async def test_snapshot_fenced_during_each_successful_and_failed_await(version, stage, mutation, code, failed, caplog):
    script = replies(version)
    if failed:
        script[stage - 1] = RuntimeError(KEY)
    service, profiles, credentials, transport = build(*script)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(verify(service))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if mutation == "revision":
            profiles.change(revision=2)
        elif mutation == "url":
            profiles.change(url=OTHER_BASE)
        elif mutation == "key":
            credentials.keys[(PROFILE, BASE)] = OTHER_KEY
        else:
            del profiles.items[PROFILE]
        resume.set()
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(task, 2)
        assert caught.value.code == code
        assert len(transport.calls) == stage
        assert set(credentials.calls) == {(PROFILE, BASE)}
        assert_secret_free(caught.value, caplog)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("version,stage", [(1, 1), (1, 2), (2, 1), (2, 2)])
async def test_cancelled_await_never_retains_exception_or_reuses_stale_result(version, stage, caplog):
    script = replies(version)
    script[stage - 1] = asyncio.CancelledError(KEY)
    service, profiles, _, transport = build(*script)

    async def change(index):
        if index == stage:
            profiles.change(revision=2)
        await asyncio.sleep(0)

    transport.hook = change
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "profile_revision_conflict"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)
    with pytest.raises(CatalogError) as stale:
        await verify(service)
    assert stale.value.code == "profile_revision_conflict"
    assert len(transport.calls) == stage


@pytest.mark.parametrize("version,stage", [(1, 1), (1, 2), (2, 1), (2, 2)])
async def test_cancellation_without_stale_binding_preserves_scrubbed_cancellation(version, stage, caplog):
    script = replies(version)
    script[stage - 1] = asyncio.CancelledError(KEY)
    service, _, _, transport = build(*script)
    with pytest.raises(asyncio.CancelledError) as caught:
        await verify(service)
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("source,problem,code", [
    ("profiles", ProfileError("profile_not_found"), "profile_not_found"),
    ("profiles", ProfileError("profile_recovery_required"), "profile_recovery_required"),
    ("profiles", OSError(KEY), "profile_store_unavailable"),
    ("credentials", CredentialsError("credential_store_corrupt"), "credential_store_unavailable"),
    ("credentials", RuntimeError(KEY), "credential_store_unavailable"),
])
async def test_local_provider_errors_raise_existing_fixed_catalog_errors(source, problem, code, caplog):
    service, profiles, credentials, transport = build()
    (profiles if source == "profiles" else credentials).error = problem
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == code and transport.calls == []
    if source == "profiles":
        assert credentials.calls == []
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("token,code", [(None, "credential_required"), ("", "invalid_credential"),
                                        (" ", "invalid_credential"), ("bad\nkey", "invalid_credential"),
                                        (1, "invalid_credential")])
async def test_missing_or_invalid_local_key_is_not_an_offline_status(token, code):
    service, _, credentials, transport = build()
    credentials.keys[(PROFILE, BASE)] = token
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == code and transport.calls == []


@pytest.mark.parametrize("changes", [{"revision": 2}, {"url": OTHER_BASE}])
async def test_profile_changed_synchronously_by_credential_reader_is_fenced(changes):
    service, profiles, credentials, transport = build()
    credentials.hook = lambda: profiles.change(**changes)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "profile_revision_conflict"
    assert transport.calls == [] and credentials.calls == [(PROFILE, BASE)]


@pytest.mark.parametrize("version,stage", [(1, 1), (1, 2), (2, 1), (2, 2)])
@pytest.mark.parametrize("failure,state,code", [
    (RuntimeError(KEY), "offline", "transport_unavailable"),
    (response({}, 302, headers={"Location": OTHER_BASE + "/" + KEY}), "error", "redirect_refused"),
    (response({}, 307, headers={"Location": BASE + "/api/v1/me?token=" + KEY}), "error", "redirect_refused"),
])
async def test_remote_exceptions_and_redirects_are_sanitized_statuses(version, stage, failure, state, code, caplog):
    script = replies(version)
    script[stage - 1] = failure
    service, _, _, transport = build(*script)
    status = await verify(service)
    assert_status(status, state, code)
    assert status.user is None and len(transport.calls) == stage
    assert all(call["url"].startswith(BASE + "/") for call in transport.calls)
    assert_secret_free(status, caplog)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("surface", ["health", "user_id", "user_name", "user_color"])
async def test_successful_secret_echo_is_rejected_before_return_or_auth(version, surface, caplog):
    script = replies(version)
    stage = 1 if surface == "health" else 2
    if surface == "health":
        script[0].body["version"] = KEY
    else:
        field = {"user_id": "id", "user_name": "name" if version == 1 else "display_name",
                 "user_color": "color"}[surface]
        script[1].body["user"][field] = KEY
    service, _, _, transport = build(*script)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "invalid_response"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


async def test_public_http_profile_verifies_the_exact_endpoint_and_key(monkeypatch):
    endpoint = "http://192.0.2.10/deployment/prefix"
    monkeypatch.setitem(globals(), "BASE", endpoint)
    service, profiles, credentials, transport = build(*replies(2))
    status = await verify(service)
    assert_status(status, "valid", None)
    assert [call["url"] for call in transport.calls] == [endpoint + "/healthz", endpoint + "/api/v2/me"]
    assert "Authorization" not in transport.calls[0]["headers"]
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer " + KEY


async def test_verify_does_not_cache_old_identity_or_catalog_permission():
    service, _, _, transport = build(*replies(2), *replies(1))
    first, second = await verify(service), await verify(service)
    assert first.catalog_version == 2
    assert second.catalog_version is None and second.error_code == "catalog_unavailable"
    assert [call["url"] for call in transport.calls] == [BASE + suffix for suffix in
        ("/healthz", "/api/v2/me", "/healthz", "/api/v1/me")]


def test_mandatory_injection_and_narrow_profile_only_api():
    constructor = inspect.signature(CatalogVerificationService)
    assert set(constructor.parameters) == {"profiles", "credentials", "transport", "clock"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in constructor.parameters.values())
    service, _, _, _ = build()
    signature = inspect.signature(service.verify)
    assert set(signature.parameters) == {"profile_id", "expected_revision"}
    assert signature.parameters["profile_id"].default is inspect.Parameter.empty
    assert signature.parameters["expected_revision"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["expected_revision"].default is inspect.Parameter.empty
    for name in ("list_projects", "get_project", "create_project", "select_project", "connect",
                 "enable", "activate", "save", "recover", "configure"):
        assert not hasattr(service, name)
    with pytest.raises(TypeError):
        CatalogVerificationService(profiles=Profiles(), credentials=Credentials(), transport=Transport())


async def test_arbitrary_url_is_not_a_profile_namespace():
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError) as caught:
        await service.verify(OTHER_BASE, expected_revision=1)
    assert caught.value.code in {"profile_not_found", "invalid_profile_metadata"}
    assert credentials.calls == transport.calls == []


@pytest.mark.parametrize("changes", [
    {"profile_id": OTHER_PROFILE}, {"profile_id": "not-a-profile"},
    {"revision": True}, {"revision": 0}, {"revision": "1"},
    {"url": "https://user:secret@verify.invalid"}, {"url": None},
])
async def test_corrupt_metadata_is_rejected_before_any_credential_lookup(changes):
    service, profiles, credentials, transport = build()
    profiles.change(**changes)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "invalid_profile_metadata"
    assert credentials.calls == transport.calls == []


@pytest.mark.parametrize("bad_reply", [
    None, {}, CatalogResponse(status=True, body=health()),
    CatalogResponse(status=99, body=health()), CatalogResponse(status="200", body=health()),
])
async def test_malformed_transport_result_is_typed_not_raw_exception(bad_reply, caplog):
    service, _, _, transport = build(bad_reply)
    status = await verify(service)
    assert_status(status, "incompatible", "invalid_response")
    assert status.user is None and len(transport.calls) == 1
    assert_secret_free(status, caplog)


async def test_verify_never_attempts_filesystem_mutation(monkeypatch):
    import builtins
    import io
    import os
    from pathlib import Path

    service, profiles, credentials, transport = build(*replies(1), *replies(2))
    original_profiles = deepcopy(profiles.items)
    original_credentials = deepcopy(credentials.keys)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Read-only Verify attempted filesystem mutation")

    def guard_open(original):
        def opened(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return original(file, mode, *args, **kwargs)
        return opened

    os_open = os.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard_open(builtins.open))
        patch.setattr(io, "open", guard_open(io.open))
        patch.setattr(os, "open", guarded_os_open)
        for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "touch"):
            patch.setattr(Path, name, forbidden)
        await verify(service)
        await verify(service)
    assert profiles.items == original_profiles
    assert credentials.keys == original_credentials
    assert len(transport.calls) == 4


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("bad_time", [None, "2026-11-15T12:30:45Z", 123,
                                       datetime(2026, 11, 15, 12, 30, 45)])
async def test_invalid_or_naive_injected_clock_raises_fixed_local_error(version, bad_time, caplog):
    service, _, _, transport = build(*replies(version), clock=lambda: bad_time)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "invalid_clock"
    assert len(transport.calls) == 2
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("version", [1, 2])
async def test_injected_clock_exception_is_not_retained_or_logged(version, caplog):
    def clock():
        raise RuntimeError(KEY)

    service, _, _, transport = build(*replies(version), clock=clock)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == "invalid_clock"
    assert len(transport.calls) == 2
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("mutation,code", [("revision", "profile_revision_conflict"),
                                          ("url", "profile_revision_conflict"),
                                          ("key", "credential_binding_conflict")])
@pytest.mark.parametrize("clock_outcome", ["valid", "invalid", "throws"])
async def test_final_fence_rejects_binding_changes_inside_injected_clock(version, mutation, code, clock_outcome, caplog):
    def clock():
        if mutation == "revision":
            profiles.change(revision=2)
        elif mutation == "url":
            profiles.change(url=OTHER_BASE)
        else:
            credentials.keys[(PROFILE, BASE)] = OTHER_KEY
        if clock_outcome == "throws":
            raise RuntimeError(KEY)
        return NOW if clock_outcome == "valid" else None

    service, profiles, credentials, transport = build(*replies(version), clock=clock)
    with pytest.raises(CatalogError) as caught:
        await verify(service)
    assert caught.value.code == code
    assert len(transport.calls) == 2
    assert set(credentials.calls) == {(PROFILE, BASE)}
    assert_secret_free(caught.value, caplog)

"""Synthetic, unmounted profile-bound Create (studio-design §5.1/§6/S3).

All identities, keys, responses and interleavings are in memory. The accepted
socket-free loop and I/O counters also catch swallowed forbidden attempts. Run
only under the task's isolated home/temp diagnostic harness, never a server.
These default instance-mode tests deliberately make no restart guarantee.
Explicit durable integration is covered by test_remote_catalog_create_durable.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import inspect
from pathlib import Path

import pytest

from voxarium_studio.sync.catalog import CatalogClient, CatalogError, CreateResult, Verification
from voxarium_studio.sync.catalog_create_service import CatalogCreateService
from voxarium_studio.sync.catalog_service import CatalogService
from voxarium_studio.sync.credentials import CredentialsError
from voxarium_studio.sync.profiles import ProfileError

from test_remote_catalog_service import (
    BASE_A, BASE_B, CAPABILITIES, EPOCH, KEY_A, KEY_B, PROFILE_A, PROFILE_B,
    Credentials, Profiles, Transport, assert_secret_free, event_loop_policy,
    health, identity, project, response, synthetic_only,
)
from test_remote_catalog_read_service import external_io_counters


OP = "synthetic-create-113"
LABEL = "  Cafe\u0301 / Project  "
WIRE_LABEL = "Caf\u00e9 / Project"
ACTIONS = ("create", "recover")
MUTATIONS = [
    ("revision", "profile_revision_conflict"),
    ("url", "profile_revision_conflict"),
    ("key", "credential_binding_conflict"),
    ("delete", "profile_not_found"),
    ("profile_id", "invalid_profile_metadata"),
    ("missing_key", "credential_required"),
    ("profile_error", "profile_store_unavailable"),
    ("credential_error", "credential_store_unavailable"),
    ("recovery", "profile_recovery_required"),
]


def build(*replies):
    profiles, credentials, transport = Profiles(), Credentials(), Transport(*replies)
    service = CatalogCreateService(profiles=profiles, credentials=credentials, transport=transport)
    return service, profiles, credentials, transport


def envelope(id="alpha", **changes):
    return {"protocol": 2, "project": project(id, display_name=WIRE_LABEL, **changes)}


def success(action="create", *, id="alpha"):
    return [response(health()), response(identity()),
            response(envelope(id), status=201 if action == "create" else 200)]


def failure(code, status):
    return response({"error": {"code": code, "message": KEY_A, "request_id": KEY_B}}, status)


async def create(service, *, profile_id=PROFILE_A, revision=1, id="alpha",
                 display_name=LABEL, operation_id=OP):
    return await service.create_project(profile_id, expected_revision=revision, id=id,
                                        display_name=display_name, operation_id=operation_id)


async def invoke(service, action, *, profile_id=PROFILE_A, revision=1, operation_id=OP):
    if action == "create":
        return await create(service, profile_id=profile_id, revision=revision, operation_id=operation_id)
    return await service.recover_create(profile_id, operation_id, expected_revision=revision)


async def prepare(action):
    """Give recovery an uncertain original POST, not an invented/rebound record."""
    service, profiles, credentials, transport = build()
    if action == "recover":
        transport.replies[:] = [response(health()), response(identity()), RuntimeError(KEY_A)]
        result = await create(service)
        assert result.state == "unknown"
        assert sum(c["method"] == "POST" for c in transport.calls) == 1
        transport.calls.clear()
    return service, profiles, credentials, transport


def mutate(profiles, credentials, mutation):
    if mutation == "revision":
        profiles.change(revision=2)
    elif mutation == "url":
        profiles.change(url=BASE_B)
    elif mutation == "key":
        credentials.keys[(PROFILE_A, BASE_A)] = KEY_B
    elif mutation == "delete":
        del profiles.items[PROFILE_A]
    elif mutation == "profile_id":
        profiles.change(profile_id=PROFILE_B)
    elif mutation == "missing_key":
        credentials.keys.pop((PROFILE_A, BASE_A))
    elif mutation == "profile_error":
        profiles.error = OSError(KEY_A)
    elif mutation == "credential_error":
        credentials.error = CredentialsError(KEY_A)
    else:
        profiles.error = ProfileError("profile_recovery_required")


def assert_dto_safe(result, caplog=None):
    assert isinstance(result, (CreateResult, Verification))
    for key in (KEY_A, KEY_B):
        assert key not in repr(result)
        assert key not in repr(asdict(result))
        if caplog is not None:
            assert key not in caplog.text
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        result.synthetic_mutation = True


def test_mandatory_injection_and_narrow_api():
    signature = inspect.signature(CatalogCreateService)
    assert set(signature.parameters) == {"profiles", "credentials", "transport", "mode", "journal"}
    for name, parameter in signature.parameters.items():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        if name in {"profiles", "credentials", "transport"}:
            assert parameter.default is inspect.Parameter.empty
    assert signature.parameters["mode"].default == "instance"
    assert signature.parameters["journal"].default is None
    for missing in ("profiles", "credentials", "transport"):
        kwargs = {"profiles": Profiles(), "credentials": Credentials(), "transport": Transport()}
        del kwargs[missing]
        with pytest.raises(TypeError):
            CatalogCreateService(**kwargs)
    service, _, _, _ = build()
    expected = {
        "verify": {"profile_id", "expected_revision"},
        "create_project": {"profile_id", "expected_revision", "id", "display_name", "operation_id"},
        "recover_create": {"profile_id", "operation_id", "expected_revision"},
    }
    for name, parameters in expected.items():
        method = getattr(service, name)
        assert inspect.iscoroutinefunction(method)
        actual = inspect.signature(method).parameters
        assert set(actual) == parameters
        assert all(p.default is inspect.Parameter.empty for p in actual.values())
        assert actual["expected_revision"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("select_project", "select", "connect", "enable", "activate", "save", "load"):
        assert not hasattr(service, name)


async def test_reuses_profile_guards_and_wire_client(monkeypatch):
    calls = []

    def spy_sync(name):
        original = getattr(CatalogService, name)

        def wrapped(self, *args, **kwargs):
            calls.append(("guard", name))
            return original(self, *args, **kwargs)
        return wrapped

    def spy_async(name):
        original = getattr(CatalogClient, name)

        async def wrapped(self, *args, **kwargs):
            calls.append(("client", name))
            return await original(self, *args, **kwargs)
        return wrapped

    for name in ("_snapshot", "_check"):
        monkeypatch.setattr(CatalogService, name, spy_sync(name))
    for name in ("verify", "create_project", "get_project"):
        monkeypatch.setattr(CatalogClient, name, spy_async(name))
    service, _, _, transport = build(
        response(health()), response(identity()), RuntimeError(KEY_A), *success("recover"))
    await create(service)
    recovered = await invoke(service, "recover")
    assert recovered.state == "found"
    assert ("guard", "_snapshot") in calls and ("guard", "_check") in calls
    assert calls.count(("client", "verify")) == 2
    assert calls.count(("client", "create_project")) == 1
    assert calls.count(("client", "get_project")) == 1
    assert [c["method"] for c in transport.calls] == ["GET", "GET", "POST", "GET", "GET", "GET"]


async def test_verify_only_gets_and_always_reverifies(caplog):
    service, profiles, credentials, transport = build(
        response(health()), response(identity()), response(health()), response(identity()))
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    for _ in range(2):
        result = await service.verify(PROFILE_A, expected_revision=1)
        assert result.capabilities == frozenset(CAPABILITIES)
        assert_dto_safe(result, caplog)
    assert [c["url"] for c in transport.calls] == [
        BASE_A + suffix for suffix in ("/healthz", "/api/v2/me") * 2]
    assert all(c["method"] == "GET" and c["json"] is None for c in transport.calls)
    assert profiles.items == old_profiles and credentials.keys == old_keys


async def test_two_profiles_keep_exact_url_key_and_normalized_wire_label(caplog):
    service, profiles, credentials, transport = build(*success(), *success())
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    first = await create(service)
    second = await create(service, profile_id=PROFILE_B, operation_id="other-profile")
    assert first.state == second.state == "created"
    assert first.project.id == second.project.id == "alpha"
    assert first.project.space_instance_id == second.project.space_instance_id == EPOCH
    for result in (first, second):
        assert_dto_safe(result, caplog)
    for offset, (base, key) in enumerate(((BASE_A, KEY_A), (BASE_B, KEY_B))):
        calls = transport.calls[offset * 3:(offset + 1) * 3]
        assert [c["url"] for c in calls] == [base + p for p in ("/healthz", "/api/v2/me", "/api/v2/projects")]
        assert [c["method"] for c in calls] == ["GET", "GET", "POST"]
        for index, call in enumerate(calls):
            assert call["follow_redirects"] is False
            assert call["headers"]["Cache-Control"] == "no-store"
            assert "Cookie" not in call["headers"]
            assert call["params"] == {}
            if index == 0:
                assert "Authorization" not in call["headers"]
            else:
                assert call["headers"]["Authorization"] == "Bearer " + key
            assert call["json"] == ({"id": "alpha", "display_name": WIRE_LABEL} if index == 2 else None)
            assert OP not in repr((call["url"], call["params"], call["json"], call["headers"]))
            assert key not in repr((call["url"], call["params"], call["json"]))
    assert set(credentials.calls) == {(PROFILE_A, BASE_A), (PROFILE_B, BASE_B)}
    assert profiles.items == old_profiles and credentials.keys == old_keys
    assert KEY_A not in repr(service) and KEY_B not in repr(service)
    assert await create(service) == first
    assert len(transport.calls) == 6


@pytest.mark.parametrize("changed", [
    {"profile_id": PROFILE_B}, {"revision": 2}, {"revision": True},
    {"id": "beta"}, {"display_name": WIRE_LABEL}, {"display_name": "different"},
])
async def test_operation_id_is_global_and_binds_exact_raw_parameters(changed):
    service, _, _, transport = build(*success())
    await create(service)
    with pytest.raises(CatalogError, match="^operation_conflict$") as caught:
        await create(service, **changed)
    assert len(transport.calls) == 3
    assert_secret_free(caught.value)


@pytest.mark.parametrize("profile_id,revision", [(PROFILE_B, 1), (PROFILE_A, 2), (PROFILE_A, True)])
async def test_recovery_cannot_change_operation_profile_or_revision(profile_id, revision):
    service, _, _, transport = await prepare("recover")
    with pytest.raises(CatalogError, match="^operation_conflict$"):
        await invoke(service, "recover", profile_id=profile_id, revision=revision)
    assert transport.calls == []


@pytest.mark.parametrize("value", [None, True, 113, "", " ", "a/b", "a b", "x\n", "é", "a" * 129])
@pytest.mark.parametrize("action", ACTIONS)
async def test_invalid_operation_id_never_reaches_transport(value, action):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^invalid_operation_id$"):
        await invoke(service, action, operation_id=value)
    assert transport.calls == [] and credentials.calls == []


@pytest.mark.parametrize("operation_id", ["a", "A0._-", "a" * 128])
async def test_operation_id_accepts_exact_client_regex(operation_id):
    service, _, _, transport = build(*success())
    assert (await create(service, operation_id=operation_id)).state == "created"
    assert len(transport.calls) == 3
    assert transport.calls[2]["json"] == {"id": "alpha", "display_name": WIRE_LABEL}


@pytest.mark.parametrize("id", [None, True, 113, "", "Alpha", " alpha", "a/b", "é", "a" * 65])
async def test_invalid_project_id_never_posts(id):
    service, _, _, transport = build(*success())
    with pytest.raises(CatalogError, match="^invalid_id$"):
        await create(service, id=id)
    assert all(c["method"] == "GET" for c in transport.calls)


@pytest.mark.parametrize("label", [
    None, True, 113, "", "   ", "x\n", "\tname", "a\x7fb", "a\x85b",
    "a\u2028b", "a\u2029b", "a\u202eb", "a\u2066b", "\ud800", "a" * 121,
])
async def test_invalid_label_uses_wire_validation_without_post(label):
    service, _, _, transport = build(*success())
    with pytest.raises(CatalogError, match="^invalid_display_name$"):
        await create(service, display_name=label)
    assert all(c["method"] == "GET" for c in transport.calls)


async def test_unknown_operation_cannot_be_recovered_or_invented():
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^operation_not_found$"):
        await invoke(service, "recover")
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("mutation,code", MUTATIONS)
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_every_await_fences_and_latches_binding_failure(action, stage, mutation, code, outcome, caplog):
    service, profiles, credentials, transport = await prepare(action)
    saved_profiles, saved_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    transport.replies[:] = success(action)
    if outcome != "success":
        transport.replies[stage - 1] = (RuntimeError(KEY_A) if outcome == "failure"
                                       else asyncio.CancelledError(KEY_A))

    async def change(index):
        if index == stage:
            mutate(profiles, credentials, mutation)
        await asyncio.sleep(0)

    transport.hook = change
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == code
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)
    # A restored provider must not erase an already detected stale operation.
    profiles.items, credentials.keys = saved_profiles, saved_keys
    transport.hook = None
    before = len(transport.calls)
    for retry in ACTIONS:
        with pytest.raises(CatalogError) as repeated:
            await invoke(service, retry)
        assert repeated.value.code == code
        assert_secret_free(repeated.value, caplog)
    assert len(transport.calls) == before
    assert set(credentials.calls) <= {(PROFILE_A, BASE_A)}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("mutation,code", MUTATIONS)
async def test_changed_binding_is_fenced_before_next_await(action, mutation, code):
    service, profiles, credentials, transport = await prepare("recover")
    mutate(profiles, credentials, mutation)
    with pytest.raises(CatalogError, match="^" + code + "$"):
        await invoke(service, action)
    assert transport.calls == []


async def test_sync_credential_provider_side_effect_is_fenced_before_transport():
    service, profiles, credentials, transport = build()
    credentials.hook = lambda: profiles.change(revision=2)
    with pytest.raises(CatalogError, match="^profile_revision_conflict$"):
        await create(service)
    credentials.hook = None
    profiles.change(revision=1)
    with pytest.raises(CatalogError, match="^profile_revision_conflict$"):
        await create(service)
    assert transport.calls == []


@pytest.mark.parametrize("stage", [1, 2])
async def test_failed_preflight_is_reserved_cached_and_recoverable_only_by_get(stage, caplog):
    replies = success()
    replies[stage - 1] = RuntimeError(KEY_A)
    service, _, _, transport = build(*replies)
    for _ in range(2):
        with pytest.raises(CatalogError, match="^transport_unavailable$") as caught:
            await create(service)
        assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    assert all(c["method"] == "GET" for c in transport.calls)
    transport.replies[:] = success("recover")
    recovered = await invoke(service, "recover")
    assert recovered.state == "found" and recovered.project.id == "alpha"
    assert all(c["method"] == "GET" for c in transport.calls)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_cancelled_await_scrubs_canary_reserves_operation_and_recovery_is_get_only(action, stage, caplog):
    service, _, _, transport = await prepare(action)
    transport.replies[:] = success(action)
    transport.replies[stage - 1] = asyncio.CancelledError(KEY_A)
    with pytest.raises(asyncio.CancelledError) as caught:
        await invoke(service, action)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    before_posts = sum(c["method"] == "POST" for c in transport.calls)
    before = len(transport.calls)
    repeated = await create(service)
    assert repeated.state == "unknown"
    assert len(transport.calls) == before
    transport.replies[:] = success("recover")
    recovered = await invoke(service, "recover")
    assert recovered.state == "found"
    assert sum(c["method"] == "POST" for c in transport.calls) == before_posts
    assert all(c["json"] is None for c in transport.calls[before:])
    assert_dto_safe(recovered, caplog)


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_actual_task_cancellation_does_not_release_reservation(stage):
    service, _, _, transport = build(*success())
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    first = asyncio.create_task(create(service))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        first.cancel(KEY_A)
        with pytest.raises(asyncio.CancelledError) as caught:
            await first
        assert_secret_free(caught.value)
        transport.hook = None
        before = len(transport.calls)
        assert (await create(service)).state == "unknown"
        assert len(transport.calls) == before
        transport.replies[:] = success("recover")
        assert (await invoke(service, "recover")).state == "found"
        assert all(c["method"] == "GET" for c in transport.calls[before:])
    finally:
        resume.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("stage", [1, 2, 3])
async def test_reservation_exists_before_first_await_and_concurrent_recovery_is_busy(stage):
    service, _, _, transport = build(*success())
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    first = asyncio.create_task(create(service))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        concurrent = await asyncio.wait_for(create(service), timeout=2)
        assert concurrent.state == "unknown"
        assert len(transport.calls) == stage
        with pytest.raises(CatalogError, match="^operation_in_progress$"):
            await invoke(service, "recover")
        with pytest.raises(CatalogError, match="^operation_conflict$"):
            await create(service, display_name=WIRE_LABEL)
        assert len(transport.calls) == stage
        resume.set()
        result = await asyncio.wait_for(first, timeout=2)
        assert result.state == "created"
        assert await create(service) == result
        assert len(transport.calls) == 3
        assert sum(c["method"] == "POST" for c in transport.calls) == 1
    finally:
        resume.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_concurrent_profiles_do_not_rebind_waiting_post():
    service, _, _, transport = build()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def routed(method, url, *, headers, params, json, follow_redirects):
        transport.calls.append({"method": method, "url": url, "headers": dict(headers),
                                "params": dict(params), "json": deepcopy(json),
                                "follow_redirects": follow_redirects})
        if url == BASE_A + "/api/v2/projects":
            entered.set()
            await resume.wait()
        if url.endswith("/healthz"):
            assert "Authorization" not in headers
            return response(health())
        key = KEY_A if url.startswith(BASE_A + "/") else KEY_B
        assert headers["Authorization"] == "Bearer " + key
        assert follow_redirects is False
        if url.endswith("/api/v2/me"):
            return response(identity())
        assert method == "POST" and json == {"id": "alpha", "display_name": WIRE_LABEL}
        return response(envelope(), status=201)

    transport.request = routed
    first = asyncio.create_task(create(service))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await asyncio.wait_for(create(service, profile_id=PROFILE_B, operation_id="profile-b"), timeout=2)
        assert second.state == "created"
        resume.set()
        assert (await asyncio.wait_for(first, timeout=2)).state == "created"
        assert len(transport.calls) == 6
        assert sum(c["method"] == "POST" for c in transport.calls) == 2
    finally:
        resume.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("reply,state,code", [
    (RuntimeError(KEY_A), "unknown", "transport_unavailable"),
    (failure("project_exists", 409), "conflict", "project_exists"),
    (failure("temporarily_unavailable", 503), "unknown", "temporarily_unavailable"),
    (failure("unauthorised", 401), "rejected", "unauthorised"),
    (failure("invalid_id", 400), "rejected", "invalid_id"),
    (response({}, status=307, headers={"Location": BASE_B}), "unknown", "redirect_refused"),
    (response({}, status=201), "unknown", "invalid_response"),
    (response({"protocol": 2, "project": project("different")}, status=201), "unknown", "identity_mismatch"),
    (response({"protocol": 1, "project": project()}, status=201), "unknown", "version_mismatch"),
])
async def test_post_outcomes_are_cached_and_never_blindly_retried(reply, state, code, caplog):
    service, _, _, transport = build(response(health()), response(identity()), reply)
    result = await create(service)
    assert (result.state, result.error_code) == (state, code)
    assert result.project is None
    assert_dto_safe(result, caplog)
    for _ in range(2):
        assert await create(service) == result
    assert len(transport.calls) == 3
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


@pytest.mark.parametrize("initial", [RuntimeError(KEY_A), failure("project_exists", 409)])
@pytest.mark.parametrize("get_reply,state,code", [
    (response(envelope()), "found", None),
    (failure("project_not_found", 404), "absent", "project_not_found"),
])
async def test_unknown_and_409_recover_only_original_id_never_select_or_repost(initial, get_reply, state, code, caplog):
    service, profiles, credentials, transport = build(
        response(health()), response(identity()), initial,
        response(health()), response(identity()), get_reply)
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    await create(service)
    recovered = await invoke(service, "recover")
    assert (recovered.state, recovered.error_code) == (state, code)
    assert_dto_safe(recovered, caplog)
    if state == "found":
        assert recovered.project.id == "alpha"
    else:
        assert recovered.project is None
    assert [c["url"] for c in transport.calls[3:]] == [
        BASE_A + p for p in ("/healthz", "/api/v2/me", "/api/v2/projects/alpha")]
    assert all(c["method"] == "GET" and c["json"] is None for c in transport.calls[3:])
    before = len(transport.calls)
    assert await create(service) == recovered
    assert len(transport.calls) == before
    assert profiles.items == old_profiles and credentials.keys == old_keys
    assert sum(c["method"] == "POST" for c in transport.calls) == 1


@pytest.mark.parametrize("get_reply,code", [
    (failure("unauthorised", 401), "unauthorised"),
    (failure("temporarily_unavailable", 503), "temporarily_unavailable"),
    (RuntimeError(KEY_A), "transport_unavailable"),
    (response({}, status=308, headers={"Location": BASE_B}), "redirect_refused"),
    (response({"protocol": 2, "project": project("different")}), "identity_mismatch"),
    (response({"protocol": 1, "project": project()}), "version_mismatch"),
    (response({}), "invalid_response"),
])
async def test_failed_recovery_returns_no_observation_and_cannot_enable_post(get_reply, code, caplog):
    service, _, _, transport = await prepare("recover")
    transport.replies[:] = [response(health()), response(identity()), get_reply]
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, "recover")
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == 3
    assert all(c["method"] == "GET" and c["json"] is None for c in transport.calls)
    before = len(transport.calls)
    assert (await create(service)).state == "unknown"
    assert len(transport.calls) == before
    # A subsequent explicit recovery has fresh verification, not a blind POST.
    transport.replies[:] = success("recover")
    assert (await invoke(service, "recover")).state == "found"
    assert all(c["method"] == "GET" for c in transport.calls)


async def test_absent_recovery_may_be_read_again_but_never_reposts():
    service, _, _, transport = await prepare("recover")
    for reply, expected in ((failure("project_not_found", 404), "absent"),
                            (response(envelope()), "found")):
        transport.replies[:] = [response(health()), response(identity()), reply]
        assert (await invoke(service, "recover")).state == expected
        assert (await create(service)).state == expected
    assert len(transport.calls) == 6
    assert all(c["method"] == "GET" and c["json"] is None for c in transport.calls)


@pytest.mark.parametrize("action", ["verify", "create", "recover"])
@pytest.mark.parametrize("stage,bad_reply,code", [
    (1, response(health(protocol=1)), "version_mismatch"),
    (1, response({"protocol": 2}), "invalid_response"),
    (1, response({}, status=302, headers={"Location": BASE_B}), "redirect_refused"),
    (2, failure("unauthorised", 401), "unauthorised"),
    (2, response(identity(capabilities=[])), "capabilities_missing"),
    (2, response(identity(catalog_api=1)), "version_mismatch"),
    (2, response({}, status=307, headers={"Location": BASE_B}), "redirect_refused"),
])
async def test_preflight_auth_version_capability_redirect_fail_closed(action, stage, bad_reply, code, caplog):
    service, _, _, transport = await prepare("recover" if action == "recover" else "create")
    transport.replies[:] = success()
    transport.replies[stage - 1] = bad_reply
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        if action == "verify":
            await service.verify(PROFILE_A, expected_revision=1)
        else:
            await invoke(service, action)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    assert all(c["method"] == "GET" and c["url"].startswith(BASE_A + "/") for c in transport.calls)
    if action == "create":
        with pytest.raises(CatalogError, match="^" + code + "$"):
            await create(service)
        assert len(transport.calls) == stage


@pytest.mark.parametrize("action", ["verify", "create"])
@pytest.mark.parametrize("revision", [0, 2, True, "1", None])
async def test_bad_revision_is_rejected_before_credentials(action, revision):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^profile_revision_conflict$"):
        if action == "verify":
            await service.verify(PROFILE_A, expected_revision=revision)
        else:
            await create(service, revision=revision)
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("source,problem,code", [
    ("profiles", ProfileError("profile_not_found"), "profile_not_found"),
    ("profiles", ProfileError("profile_recovery_required"), "profile_recovery_required"),
    ("profiles", OSError(KEY_A), "profile_store_unavailable"),
    ("credentials", CredentialsError("credential_store_corrupt"), "credential_store_unavailable"),
    ("credentials", RuntimeError(KEY_A), "credential_store_unavailable"),
])
async def test_initial_provider_failure_is_fixed_reserved_and_cannot_rebind(source, problem, code, caplog):
    service, profiles, credentials, transport = build()
    provider = profiles if source == "profiles" else credentials
    provider.error = problem
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await create(service)
    assert_secret_free(caught.value, caplog)
    provider.error = None
    for retry in ACTIONS:
        with pytest.raises(CatalogError, match="^" + code + "$") as repeated:
            await invoke(service, retry)
        assert_secret_free(repeated.value, caplog)
    assert transport.calls == []


@pytest.mark.parametrize("source,problem,code", [
    ("profiles", ProfileError("profile_recovery_required"), "profile_recovery_required"),
    ("profiles", OSError(KEY_A), "profile_store_unavailable"),
    ("credentials", CredentialsError("credential_recovery_required"), "credential_store_unavailable"),
])
async def test_recovery_provider_failure_latches_even_after_store_restored(source, problem, code, caplog):
    service, profiles, credentials, transport = await prepare("recover")
    provider = profiles if source == "profiles" else credentials
    provider.error = problem
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await invoke(service, "recover")
    assert_secret_free(caught.value, caplog)
    provider.error = None
    for action in ACTIONS:
        with pytest.raises(CatalogError, match="^" + code + "$"):
            await invoke(service, action)
    assert transport.calls == []


@pytest.mark.parametrize("token,code", [
    (None, "credential_required"), ("", "invalid_credential"),
    ("bad\nkey", "invalid_credential"), (113, "invalid_credential"),
])
async def test_missing_or_invalid_key_never_falls_back(token, code):
    service, _, credentials, transport = build()
    credentials.keys[(PROFILE_A, BASE_A)] = token
    with pytest.raises(CatalogError, match="^" + code + "$"):
        await create(service)
    assert all(pair == (PROFILE_A, BASE_A) for pair in credentials.calls)
    assert transport.calls == []


@pytest.mark.parametrize("changes", [
    {"profile_id": PROFILE_B}, {"profile_id": "not-a-uuid"}, {"revision": True},
    {"revision": 0}, {"url": None}, {"url": "https://user:secret@catalog.invalid"},
])
async def test_corrupt_profile_does_not_look_up_keys(changes):
    service, profiles, credentials, transport = build()
    profiles.change(**changes)
    with pytest.raises(CatalogError, match="^invalid_profile_metadata$"):
        await create(service)
    assert transport.calls == [] and credentials.calls == []


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("mutation,code", MUTATIONS)
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_verify_fences_even_failed_and_cancelled_health_and_me(stage, mutation, code, outcome, caplog):
    service, profiles, credentials, transport = build(response(health()), response(identity()))
    if outcome != "success":
        transport.replies[stage - 1] = (RuntimeError(KEY_A) if outcome == "failure"
                                       else asyncio.CancelledError(KEY_A))

    async def change(index):
        if index == stage:
            mutate(profiles, credentials, mutation)
        await asyncio.sleep(0)

    transport.hook = change
    with pytest.raises(CatalogError, match="^" + code + "$") as caught:
        await service.verify(PROFILE_A, expected_revision=1)
    assert_secret_free(caught.value, caplog)
    assert len(transport.calls) == stage
    assert all(c["method"] == "GET" for c in transport.calls)


async def test_arbitrary_url_never_acts_as_profile_namespace():
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^profile_not_found$"):
        await create(service, profile_id="https://arbitrary.invalid/prefix")
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("raw", ["https://CATALOG.INVALID/deploy/alpha/",
                                  "wss://catalog.invalid/deploy/alpha/"])
async def test_profile_url_normalization_is_shared_with_credential_binding(raw):
    service, profiles, credentials, transport = build(*success())
    profiles.change(url=raw)
    assert (await create(service)).state == "created"
    assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
    assert [c["url"] for c in transport.calls] == [
        BASE_A + p for p in ("/healthz", "/api/v2/me", "/api/v2/projects")]


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("surface,stage", [
    ("health", 1), ("user", 2), ("capability", 2), ("project", 3), ("created_at", 3),
])
async def test_successful_secret_echo_is_invalid_response_not_dto_or_logs(action, surface, stage, caplog):
    service, _, _, transport = await prepare(action)
    transport.replies[:] = success(action)
    if surface == "health":
        transport.replies[0].body["version"] = KEY_A
    elif surface == "user":
        transport.replies[1].body["user"]["display_name"] = "prefix " + KEY_A
    elif surface == "capability":
        transport.replies[1].body["capabilities"].append(KEY_A)
    else:
        field = "display_name" if surface == "project" else "created_at"
        transport.replies[2].body["project"][field] = KEY_A
    with pytest.raises(CatalogError, match="^invalid_response$") as caught:
        await invoke(service, action)
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)
    with pytest.raises(CatalogError, match="^invalid_response$"):
        await create(service)
    assert len(transport.calls) == stage


async def test_create_and_recovery_never_write_files_or_select(monkeypatch):
    """Block filesystem mutation itself; do not merely compare a final config."""
    import builtins
    import io
    import os

    service, profiles, credentials, transport = build(
        response(health()), response(identity()), failure("project_exists", 409), *success("recover"))
    old_profiles, old_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("write")
        raise AssertionError("Synthetic Create attempted filesystem mutation")

    def guard_open(original):
        def opened(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return original(file, mode, *args, **kwargs)
        return opened

    original_os_open = os.open

    def guard_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard_open(builtins.open))
        patch.setattr(io, "open", guard_open(io.open))
        patch.setattr(os, "open", guard_os_open)
        for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "touch"):
            patch.setattr(Path, name, forbidden)
        assert (await create(service)).state == "conflict"
        assert (await invoke(service, "recover")).state == "found"
    assert attempts == []
    assert profiles.items == old_profiles and credentials.keys == old_keys
    assert len(transport.calls) == 6


@pytest.mark.parametrize("invalid,code", [
    ({"id": "INVALID"}, "invalid_id"),
    ({"display_name": "\n"}, "invalid_display_name"),
    ({"revision": True}, "profile_revision_conflict"),
    ({"revision": "1"}, "profile_revision_conflict"),
])
async def test_validation_failure_reserves_exact_intent_without_later_repair(invalid, code):
    service, _, credentials, transport = build()
    for _ in range(2):
        with pytest.raises(CatalogError, match="^" + code + "$"):
            await create(service, **invalid)
    for changed in ({}, {"profile_id": PROFILE_B}):
        with pytest.raises(CatalogError, match="^operation_conflict$"):
            await create(service, **changed)
    assert credentials.calls == [] and transport.calls == []


@pytest.mark.parametrize("stage", [1, 2])
async def test_verification_404_is_not_an_absent_project_observation(stage):
    service, _, _, transport = await prepare("recover")
    transport.replies[:] = success("recover")
    transport.replies[stage - 1] = failure("project_not_found", 404)
    with pytest.raises(CatalogError, match="^project_not_found$"):
        await invoke(service, "recover")
    assert len(transport.calls) == stage
    assert all(c["method"] == "GET" for c in transport.calls)


@pytest.mark.parametrize("code", ["project_exists", "metadata_conflict", "snapshot_changed"])
async def test_any_409_only_recovers_by_get_and_new_post_requires_new_command(code):
    service, _, _, transport = build(
        response(health()), response(identity()), failure(code, 409),
        response(health()), response(identity()), failure("project_not_found", 404),
        *success())
    await create(service)
    assert (await invoke(service, "recover")).state == "absent"
    assert (await create(service)).state == "absent"
    assert sum(c["method"] == "POST" for c in transport.calls) == 1
    assert (await create(service, operation_id="new-explicit-command")).state == "created"
    assert sum(c["method"] == "POST" for c in transport.calls) == 2


async def test_new_instance_cannot_claim_restart_recovery():
    service, profiles, credentials, transport = await prepare("recover")
    new_instance = CatalogCreateService(profiles=profiles, credentials=credentials,
                                        transport=transport)
    with pytest.raises(CatalogError, match="^operation_not_found$"):
        await invoke(new_instance, "recover")
    assert transport.calls == []
    # Never demonstrate Create replay on the new instance: durable integration
    # must supply GET-only restart recovery before any production wiring exists.


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("mutation,code", MUTATIONS[:3:2])
async def test_actual_task_cancel_still_fences_binding(action, stage, mutation, code):
    service, profiles, credentials, transport = await prepare(action)
    transport.replies[:] = success(action)
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        mutate(profiles, credentials, mutation)
        task.cancel(KEY_A)
        with pytest.raises(CatalogError, match="^" + code + "$") as caught:
            await task
        assert_secret_free(caught.value)
        assert len(transport.calls) == stage
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

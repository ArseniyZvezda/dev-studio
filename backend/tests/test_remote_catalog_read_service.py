"""Synthetic profile-bound snapshot/legacy observations (studio-design §6/S3).

All providers, keys, responses and await interleavings are in memory. Reuse the
accepted socket-free event loop and store/network guards; never construct a real
transport or context. Run only with an isolated home/temp/cache (VS-111).
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
import inspect
from pathlib import Path
import socket

import pytest

from voxarium_studio.sync.catalog import CatalogError
from voxarium_studio.sync.catalog_read_service import CatalogReadService
from voxarium_studio.sync.catalog_reads import LegacyResolution, ProjectSnapshot
from voxarium_studio.sync.credentials import CredentialsError
from voxarium_studio.sync.profiles import ProfileError

from test_remote_catalog_service import (
    BASE_A, BASE_B, CAPABILITIES, EPOCH, KEY_A, KEY_B, PROFILE_A, PROFILE_B,
    Credentials, Profiles, Transport, assert_secret_free, event_loop_policy,
    health, identity, response, synthetic_only,
)
from test_remote_catalog_reads import document, legacy, page


CAPS = CAPABILITIES + ["project_snapshot", "legacy_resolution"]
NAME = "  LeGaCy / e\u0301 / Проект  "
ACTIONS = ("snapshot", "legacy")
STAGES = [("snapshot", n) for n in range(1, 6)] + [("legacy", n) for n in range(1, 4)]


@pytest.fixture(autouse=True)
def external_io_counters(monkeypatch, synthetic_only):
    """Count forbidden attempts, even when production code swallows an error."""
    counts = {"dns": 0, "socket": 0, "store": 0}

    def forbidden(kind):
        def fail(*_args, **_kwargs):
            counts[kind] += 1
            raise AssertionError("Synthetic read facade attempted external I/O")
        return fail

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden("dns"))
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden("socket"))
    from voxarium_studio.core import application_config
    from voxarium_studio.sync import credentials, profiles

    for name in ("_load_application_config_strict", "_write_application_profiles",
                 "application_config_transaction"):
        monkeypatch.setattr(application_config, name, forbidden("store"))
    monkeypatch.setattr(profiles.RemoteProfiles, "get", forbidden("store"))
    for name in ("load_profile_token", "save_profile_token", "load_token", "save_token",
                 "import_legacy_token"):
        monkeypatch.setattr(credentials, name, forbidden("store"))
    yield counts
    assert counts == {"dns": 0, "socket": 0, "store": 0}


def build(*replies):
    profiles, credentials, transport = Profiles(), Credentials(), Transport(*replies)
    service = CatalogReadService(profiles=profiles, credentials=credentials, transport=transport)
    return service, profiles, credentials, transport


def success(action, *, name=NAME, id="alpha"):
    replies = [response(health()), response(identity(capabilities=CAPS.copy()))]
    if action == "snapshot":
        replies += [response(page(document(path), next_after=cursor, id=id))
                    for path, cursor in (("a.md", "a.md"), ("b.md", "b.md"), ("c.md", None))]
    else:
        replies += [response(legacy(name, id=id))]
    return replies


async def invoke(service, action, *, profile_id=PROFILE_A, revision=1, **kwargs):
    if action == "snapshot":
        return await service.snapshot(profile_id, kwargs.pop("id", "alpha"),
                                      expected_revision=revision, limit=kwargs.pop("limit", 1),
                                      **kwargs)
    return await service.resolve_legacy(profile_id, kwargs.pop("name", NAME),
                                        expected_revision=revision, **kwargs)


def failure(code, status):
    return response({"error": {"code": code, "message": KEY_A, "request_id": KEY_B}}, status)


@pytest.mark.parametrize("action", ACTIONS)
async def test_two_profiles_keep_prefix_key_and_exact_selectors(action):
    service, profiles, credentials, transport = build(*success(action), *success(action))
    before_profiles, before_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    results = [await invoke(service, action, profile_id=p) for p in (PROFILE_A, PROFILE_B)]
    size = 5 if action == "snapshot" else 3
    tail = "/api/v2/projects/alpha/snapshot" if action == "snapshot" else "/api/v2/legacy-spaces/resolve"
    for offset, (base, key) in enumerate(((BASE_A, KEY_A), (BASE_B, KEY_B))):
        calls = transport.calls[offset * size:(offset + 1) * size]
        assert [c["url"] for c in calls] == [base + "/healthz", base + "/api/v2/me"] + [base + tail] * (size - 2)
        for index, call in enumerate(calls):
            assert call["method"] == "GET" and call["json"] is None
            assert call["follow_redirects"] is False
            assert call["headers"]["Cache-Control"] == "no-store"
            assert "Cookie" not in call["headers"]
            if index == 0:
                assert "Authorization" not in call["headers"]
            else:
                assert call["headers"]["Authorization"] == "Bearer " + key
            for secret in (KEY_A, KEY_B):
                assert secret not in repr((call["url"], call["params"], call["json"]))
    assert set(credentials.calls) == {(PROFILE_A, BASE_A), (PROFILE_B, BASE_B)}
    assert set(profiles.calls) == {PROFILE_A, PROFILE_B}
    assert profiles.items == before_profiles and credentials.keys == before_keys
    for result in results:
        assert isinstance(result, ProjectSnapshot if action == "snapshot" else LegacyResolution)
        assert KEY_A not in repr(asdict(result)) and KEY_B not in repr(asdict(result))
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            result.synthetic_mutation = True
    if action == "snapshot":
        assert results[0].id == results[1].id == "alpha"
        assert results[0].space_instance_id == EPOCH
        assert [d.path for d in results[0].documents] == ["a.md", "b.md", "c.md"]
        assert transport.calls[2]["params"] == {"limit": 1}
        assert transport.calls[3]["params"] == {"limit": 1, "after": "a.md", "expected_seq": 4, "expected_epoch": EPOCH}
        assert transport.calls[4]["params"] == {"limit": 1, "after": "b.md", "expected_seq": 4, "expected_epoch": EPOCH}
    else:
        assert all(r.legacy_space_name == NAME for r in results)
        assert transport.calls[2]["params"] == transport.calls[5]["params"] == {"name": NAME}


async def test_each_call_reverifies_no_cached_success_after_capability_failure():
    denied = [response(health()), response(identity(capabilities=CAPABILITIES.copy()))]
    service, _, _, transport = build(*success("legacy"), *denied, *success("snapshot"), *success("legacy"))
    await invoke(service, "legacy")
    with pytest.raises(CatalogError, match="^capabilities_missing$"):
        await invoke(service, "snapshot")
    await invoke(service, "snapshot")
    await invoke(service, "legacy")
    assert sum(c["url"].endswith("/healthz") for c in transport.calls) == 4
    assert sum(c["url"].endswith("/api/v2/me") for c in transport.calls) == 4
    assert len(transport.calls) == 13


async def test_concurrent_profiles_do_not_share_verification_or_rebind_waiting_page():
    service, _, credentials, transport = build()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def routed_request(method, url, *, headers, params, json, follow_redirects):
        # Routing remains an entirely synthetic fake, never a network adapter.
        transport.calls.append({"method": method, "url": url, "headers": dict(headers),
                                "params": deepcopy(params), "json": json,
                                "follow_redirects": follow_redirects})
        if url == BASE_A + "/api/v2/projects/alpha/snapshot":
            entered.set()
            await resume.wait()
        if url.endswith("/healthz"):
            return response(health())
        if url.endswith("/api/v2/me"):
            return response(identity(capabilities=CAPS.copy()))
        assert url in {BASE_A + "/api/v2/projects/alpha/snapshot",
                       BASE_B + "/api/v2/projects/beta/snapshot"}
        id = "alpha" if url.startswith(BASE_A + "/") else "beta"
        return response(page(document(content=id), id=id))

    transport.request = routed_request
    first = asyncio.create_task(invoke(service, "snapshot"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await invoke(service, "snapshot", profile_id=PROFILE_B, id="beta")
        assert second.id == second.documents[0].content == "beta"
        resume.set()
        result = await asyncio.wait_for(first, timeout=2)
        assert result.id == result.documents[0].content == "alpha"
        assert len(transport.calls) == 6
        for call in transport.calls:
            assert call["method"] == "GET" and call["json"] is None
            assert call["follow_redirects"] is False
            if call["url"].endswith("/healthz"):
                assert "Authorization" not in call["headers"]
            else:
                key = KEY_A if call["url"].startswith(BASE_A + "/") else KEY_B
                assert call["headers"]["Authorization"] == "Bearer " + key
        assert set(credentials.calls) == {(PROFILE_A, BASE_A), (PROFILE_B, BASE_B)}
    finally:
        resume.set()
        if not first.done():
            first.cancel()
        await asyncio.gather(first, return_exceptions=True)


async def test_new_explicit_call_after_partial_failure_starts_fresh_snapshot():
    replies = success("snapshot")[:3] + [failure("snapshot_changed", 409)]
    replies += [response(health()), response(identity(capabilities=CAPS.copy())),
                response(page(document("fresh.md", content="new observation"), seq=9))]
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError, match="^snapshot_changed$"):
        await invoke(service, "snapshot")
    assert len(transport.calls) == 4
    result = await invoke(service, "snapshot")
    assert result.seq == 9 and [d.path for d in result.documents] == ["fresh.md"]
    assert transport.calls[6]["params"] == {"limit": 1}


async def test_snapshot_default_limit_and_expected_epoch_are_forwarded():
    service, _, _, transport = build(response(health()), response(identity(capabilities=CAPS.copy())),
                                      response(page(document())))
    result = await service.snapshot(PROFILE_A, "alpha", expected_revision=1, expected_epoch=EPOCH)
    assert result.storage_epoch == EPOCH
    assert transport.calls[2]["params"] == {"limit": 20, "expected_epoch": EPOCH}


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("url", ["https://CATALOG.INVALID/deploy/alpha/", "wss://catalog.invalid/deploy/alpha/"])
async def test_profile_url_normalization_is_shared_with_credential_binding(action, url):
    service, profiles, credentials, transport = build(*success(action))
    profiles.change(url=url)
    await invoke(service, action)
    assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
    assert all(call["url"].startswith(BASE_A + "/") for call in transport.calls)


@pytest.mark.parametrize("action,stage", STAGES)
@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"), ("url", "profile_revision_conflict"),
    ("profile_id", "invalid_profile_metadata"), ("delete", "profile_not_found"),
    ("key", "credential_binding_conflict"), ("missing_key", "credential_required"),
    ("profile_error", "profile_store_unavailable"),
    ("credential_error", "credential_store_unavailable"),
    ("recovery", "profile_recovery_required"),
])
async def test_binding_changes_during_every_await_abort_without_continuation(action, stage, mutation, code, caplog):
    service, profiles, credentials, transport = build(*success(action))
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await resume.wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "revision":
            profiles.change(revision=2)
        elif mutation == "url":
            profiles.change(url=BASE_B)
        elif mutation == "profile_id":
            profiles.change(profile_id=PROFILE_B)
        elif mutation == "delete":
            del profiles.items[PROFILE_A]
        elif mutation == "key":
            credentials.keys[(PROFILE_A, BASE_A)] = KEY_B
        elif mutation == "missing_key":
            del credentials.keys[(PROFILE_A, BASE_A)]
        elif mutation == "profile_error":
            profiles.error = OSError(KEY_A)
        elif mutation == "credential_error":
            credentials.error = CredentialsError(KEY_A)
        else:
            profiles.error = ProfileError("profile_recovery_required")
        resume.set()
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(task, timeout=2)
        assert caught.value.code == code
        assert len(transport.calls) == stage
        assert set(credentials.calls) <= {(PROFILE_A, BASE_A)}
        assert_secret_free(caught.value, caplog)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action,stage", STAGES)
@pytest.mark.parametrize("failed_await", ["exception", "409"])
async def test_failed_await_still_fences_profile_before_returning_error(action, stage, failed_await, caplog):
    replies = success(action)
    replies[stage - 1] = RuntimeError(KEY_A) if failed_await == "exception" else failure("snapshot_changed", 409)
    service, profiles, _, transport = build(*replies)

    async def mutate(index):
        if index == stage:
            profiles.change(revision=2)
        await asyncio.sleep(0)

    transport.hook = mutate
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == "profile_revision_conflict"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("revision", [0, 2, True, "1", None])
async def test_bad_revision_fails_before_credentials_and_transport(action, revision):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^profile_revision_conflict$"):
        await invoke(service, action, revision=revision)
    assert credentials.calls == transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("changes", [
    {"profile_id": PROFILE_B}, {"profile_id": "invalid"}, {"revision": True},
    {"revision": 0}, {"url": "https://user:secret@catalog.invalid"}, {"url": None},
])
async def test_corrupt_metadata_fails_before_credential_lookup(action, changes):
    service, profiles, credentials, transport = build()
    profiles.change(**changes)
    with pytest.raises(CatalogError, match="^invalid_profile_metadata$"):
        await invoke(service, action)
    assert credentials.calls == transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("source,problem,code", [
    ("profiles", ProfileError("profile_not_found"), "profile_not_found"),
    ("profiles", ProfileError("profile_recovery_required"), "profile_recovery_required"),
    ("profiles", OSError(KEY_A), "profile_store_unavailable"),
    ("credentials", CredentialsError("credential_store_corrupt"), "credential_store_unavailable"),
    ("credentials", RuntimeError(KEY_A), "credential_store_unavailable"),
])
async def test_provider_errors_are_independent_fixed_and_redacted(action, source, problem, code, caplog):
    service, profiles, credentials, transport = build()
    (profiles if source == "profiles" else credentials).error = problem
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == code
    assert transport.calls == []
    if source == "profiles":
        assert credentials.calls == []
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("token,code", [(None, "credential_required"), ("", "invalid_credential"),
    ("bad\nkey", "invalid_credential"), (" ", "invalid_credential"), (123, "invalid_credential")])
async def test_missing_or_invalid_credentials_never_fall_back(action, token, code):
    service, _, credentials, transport = build()
    credentials.keys[(PROFILE_A, BASE_A)] = token
    with pytest.raises(CatalogError, match="^" + code + "$"):
        await invoke(service, action)
    assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
    assert transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
async def test_credential_provider_side_effect_is_fenced_before_transport(action):
    service, profiles, credentials, transport = build()
    credentials.hook = lambda: profiles.change(url=BASE_B)
    with pytest.raises(CatalogError, match="^profile_revision_conflict$"):
        await invoke(service, action)
    assert set(credentials.calls) == {(PROFILE_A, BASE_A)}
    assert transport.calls == []


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("missing", CAPS)
async def test_capabilities_fail_closed_without_guessing_from_routes(action, missing):
    caps = [c for c in CAPS if c != missing]
    relevant = "project_snapshot" if action == "snapshot" else "legacy_resolution"
    if missing not in CAPABILITIES and missing != relevant:
        # The two opt-in capabilities are independent, not a new base requirement.
        replies = success(action)
        replies[1] = response(identity(capabilities=caps))
        service, _, _, transport = build(*replies)
        await invoke(service, action)
        assert len(transport.calls) == len(replies)
    else:
        service, _, _, transport = build(response(health()), response(identity(capabilities=caps)))
        with pytest.raises(CatalogError, match="^capabilities_missing$"):
            await invoke(service, action)
        assert len(transport.calls) == 2


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("bad_reply,count,code", [
    (response(health(protocol=1)), 1, "version_mismatch"),
    (response({"protocol": 2}), 1, "invalid_response"),
    (failure("unauthorised", 401), 2, "unauthorised"),
    (response(identity(capabilities=None)), 2, "invalid_response"),
    (response(identity(protocol=1)), 2, "version_mismatch"),
])
async def test_failed_discovery_never_reaches_operation(action, bad_reply, count, code, caplog):
    replies = ([response(health())] if count == 2 else []) + [bad_reply]
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == code
    assert len(transport.calls) == count
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action,stage", STAGES)
async def test_redirect_at_any_await_is_refused_without_following(action, stage, caplog):
    replies = success(action)
    replies[stage - 1] = response({}, 307, headers={"Location": BASE_B + "/" + KEY_A})
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == "redirect_refused"
    assert len(transport.calls) == stage
    assert all(c["url"].startswith(BASE_A + "/") and c["follow_redirects"] is False for c in transport.calls)
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action,stage", STAGES)
async def test_transport_exception_is_scrubbed_and_never_retried(action, stage, caplog):
    replies = success(action)
    replies[stage - 1] = RuntimeError(KEY_A)
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == "transport_unavailable"
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action,stage", STAGES)
async def test_transport_cancellation_canary_is_scrubbed(action, stage, caplog):
    replies = success(action)
    replies[stage - 1] = asyncio.CancelledError(KEY_A)
    service, _, _, transport = build(*replies)
    with pytest.raises(asyncio.CancelledError) as caught:
        await invoke(service, action)
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("action,stage", STAGES)
async def test_real_task_cancellation_stops_at_suspended_await(action, stage, caplog):
    service, _, _, transport = build(*success(action))
    entered = asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await asyncio.Event().wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel(KEY_A)
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert len(transport.calls) == stage
        assert_secret_free(caught.value, caplog)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("action,stage", STAGES)
@pytest.mark.parametrize("mutation,code", [
    ("revision", "profile_revision_conflict"),
    ("key", "credential_binding_conflict"),
])
async def test_cancelled_await_still_checks_profile_and_credential_fence(
    action, stage, mutation, code, caplog,
):
    service, profiles, credentials, transport = build(*success(action))
    entered = asyncio.Event()

    async def pause(index):
        if index == stage:
            entered.set()
            await asyncio.Event().wait()

    transport.hook = pause
    task = asyncio.create_task(invoke(service, action))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        if mutation == "revision":
            profiles.change(revision=2)
        else:
            credentials.keys[(PROFILE_A, BASE_A)] = KEY_B
        task.cancel(KEY_A)
        # Cancellation must not bypass the post-await binding check. The fixed
        # stale code takes precedence, never a transport cancellation payload.
        with pytest.raises(CatalogError) as caught:
            await task
        assert caught.value.code == code
        assert len(transport.calls) == stage
        assert set(credentials.calls) <= {(PROFILE_A, BASE_A)}
        assert_secret_free(caught.value, caplog)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("stage", [3, 4, 5])
@pytest.mark.parametrize("bad_reply,code", [
    (failure("snapshot_changed", 409), "snapshot_changed"),
    (failure("project_not_found", 404), "project_not_found"),
    (failure("unauthorised", 401), "unauthorised"),
    (response(page(document(), id="beta")), "identity_mismatch"),
    (response(page(document(), storage_epoch="f" * 32)), "snapshot_changed"),
    (response({"protocol": 2}), "invalid_response"),
])
async def test_bad_snapshot_page_discards_accumulation_and_does_not_continue(stage, bad_reply, code, caplog):
    replies = success("snapshot")
    replies[stage - 1] = bad_reply
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, "snapshot", expected_epoch=EPOCH)
    assert caught.value.code == code
    assert len(transport.calls) == stage
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("kwargs,count,code", [
    ({"max_pages": 1}, 3, "pagination_limit"),
    ({"max_bytes": 1}, 3, "snapshot_size_limit"),
    ({"limit": 0}, 2, "invalid_request"),
    ({"limit": True}, 2, "invalid_request"),
    ({"max_pages": 0}, 2, "invalid_request"),
    ({"max_bytes": 0}, 2, "invalid_request"),
    ({"expected_epoch": "bad-epoch"}, 2, "invalid_request"),
])
async def test_snapshot_options_reach_wire_parser_and_remain_bounded(kwargs, count, code):
    service, _, _, transport = build(*success("snapshot"))
    with pytest.raises(CatalogError, match="^" + code + "$"):
        await invoke(service, "snapshot", **kwargs)
    assert len(transport.calls) == count


@pytest.mark.parametrize("id", ["Alpha", " alpha", "alpha ", "a/b", "a%2fb", "", None, 123])
async def test_project_ids_are_rejected_not_repaired(id):
    service, _, _, transport = build(*success("snapshot"))
    with pytest.raises(CatalogError, match="^invalid_id$"):
        await invoke(service, "snapshot", id=id)
    assert len(transport.calls) == 2
    assert not any("/projects/" in c["url"] for c in transport.calls)


@pytest.mark.parametrize("id", ["alpha", "a.b_c-9"])
async def test_exact_valid_id_is_not_replaced_by_label(id):
    service, _, _, transport = build(*success("snapshot", id=id))
    result = await invoke(service, "snapshot", id=id)
    assert result.id == id
    assert all(c["url"] == BASE_A + f"/api/v2/projects/{id}/snapshot" for c in transport.calls[2:])


@pytest.mark.parametrize("name", [NAME, "e\u0301", "é", "Alpha", "alpha", " ", "a/b?x=1#part"])
async def test_legacy_selector_is_preserved_exactly_without_normalisation(name):
    service, _, _, transport = build(*success("legacy", name=name))
    result = await invoke(service, "legacy", name=name)
    assert result.legacy_space_name == name
    assert transport.calls[2]["params"] == {"name": name}
    assert transport.calls[2]["url"] == BASE_A + "/api/v2/legacy-spaces/resolve"


@pytest.mark.parametrize("name", ["", "bad\nname", "bad\x7fname", None, 100, "x" * 513])
async def test_invalid_legacy_selector_never_reaches_resolve(name):
    service, _, _, transport = build(*success("legacy"))
    with pytest.raises(CatalogError, match="^invalid_request$"):
        await invoke(service, "legacy", name=name)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("bad_reply,code", [
    (failure("legacy_space_not_found", 404), "legacy_space_not_found"),
    (failure("unauthorised", 401), "unauthorised"),
    (failure("snapshot_changed", 409), "snapshot_changed"),
    (response(legacy(NAME.strip())), "identity_mismatch"),
])
async def test_legacy_failure_never_guesses_id_or_falls_back_to_create(bad_reply, code, caplog):
    replies = success("legacy")
    replies[2] = bad_reply
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, "legacy")
    assert caught.value.code == code
    assert len(transport.calls) == 3
    assert_secret_free(caught.value, caplog)


@pytest.mark.parametrize("surface", ["health", "user", "capability", "content", "path", "updated_by", "updated_at", "legacy_label", "legacy_created_at"])
async def test_successful_response_cannot_echo_key_in_dto_or_document_content(surface, caplog):
    action = "legacy" if surface.startswith("legacy_") else "snapshot"
    replies = success(action)
    if surface == "health":
        replies[0].body["version"] = KEY_A
        count = 1
    elif surface in {"user", "capability"}:
        if surface == "user":
            replies[1].body["user"]["display_name"] = KEY_A
        else:
            replies[1].body["capabilities"].append(KEY_A)
        count = 2
    elif surface.startswith("legacy_"):
        replies[2].body["project"]["display_name" if surface == "legacy_label" else "created_at"] = KEY_A
        count = 3
    else:
        # A later page proves previous accumulated documents never escape either.
        changes = {surface: KEY_A}
        if surface == "path":
            changes[surface] += ".md"
        if surface == "content":
            raw = document("b.md", content="prefix " + KEY_A + " suffix")
        else:
            raw = document("b.md", **changes) if surface != "path" else document(**changes)
        replies[3] = response(page(raw))
        count = 4
    service, _, _, transport = build(*replies)
    with pytest.raises(CatalogError) as caught:
        await invoke(service, action)
    assert caught.value.code == "invalid_response"
    assert len(transport.calls) == count
    assert_secret_free(caught.value, caplog)


def test_constructor_and_public_api_have_no_runtime_or_mutation_defaults():
    constructor = inspect.signature(CatalogReadService)
    assert set(constructor.parameters) == {"profiles", "credentials", "transport"}
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
               for p in constructor.parameters.values())
    service, _, _, _ = build()
    for name in ("snapshot", "resolve_legacy"):
        signature = inspect.signature(getattr(service, name))
        assert signature.parameters["profile_id"].default is inspect.Parameter.empty
        revision = signature.parameters["expected_revision"]
        assert revision.kind is inspect.Parameter.KEYWORD_ONLY and revision.default is inspect.Parameter.empty
        assert not {"url", "token", "headers", "home", "root", "store"} & set(signature.parameters)
    assert inspect.signature(service.snapshot).parameters["limit"].default == 20
    assert inspect.signature(service.snapshot).parameters["max_pages"].default == 100
    assert inspect.signature(service.snapshot).parameters["max_bytes"].default == 64 * 1024 * 1024
    for name in ("create_project", "select_project", "connect", "enable", "activate", "save", "recover_create"):
        assert not hasattr(service, name)
    with pytest.raises(TypeError):
        CatalogReadService(profiles=Profiles(), credentials=Credentials())


@pytest.mark.parametrize("action", ACTIONS)
async def test_arbitrary_url_is_not_a_profile_or_key_namespace(action):
    service, _, credentials, transport = build()
    with pytest.raises(CatalogError, match="^profile_not_found$"):
        await invoke(service, action, profile_id="https://arbitrary.invalid/prefix")
    assert credentials.calls == transport.calls == []


async def test_reads_do_not_attempt_filesystem_mutation(tmp_path, monkeypatch):
    import builtins
    import io
    import os

    fixture = tmp_path / "synthetic-sentinel.txt"
    fixture.write_text("synthetic only", encoding="utf-8")
    before = fixture.read_bytes()
    service, profiles, credentials, transport = build(*success("snapshot"), *success("legacy"))
    before_profiles, before_keys = deepcopy(profiles.items), deepcopy(credentials.keys)
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("write")
        raise AssertionError("Read-only facade attempted a filesystem mutation")

    def guard_open(original):
        def opened(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                forbidden()
            return original(file, mode, *args, **kwargs)
        return opened

    os_open = os.open

    def guard_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            forbidden()
        return os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", guard_open(builtins.open))
        patch.setattr(io, "open", guard_open(io.open))
        patch.setattr(os, "open", guard_os_open)
        for name in ("mkdir", "unlink", "rename", "replace", "rmdir", "touch"):
            patch.setattr(Path, name, forbidden)
        for name in ("mkdir", "makedirs", "remove", "unlink", "rename", "replace", "rmdir"):
            patch.setattr(os, name, forbidden)
        await invoke(service, "snapshot")
        await invoke(service, "legacy")
    assert attempts == []
    assert fixture.read_bytes() == before
    assert profiles.items == before_profiles and credentials.keys == before_keys
    assert len(transport.calls) == 8

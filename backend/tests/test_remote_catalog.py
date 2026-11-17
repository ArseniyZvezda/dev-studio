"""Isolated S3 contracts (studio-design §5.1/§6; server-design §3).

Only a scripted transport is used: no server, DNS, credentials or project data.
Create is an explicit local action, not a selection or server replay protocol.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import traceback

import pytest

from voxarium_studio.sync.catalog import CatalogClient, CatalogError, CatalogResponse


BASE = "https://catalog.invalid/deployment/team"
KEY = "SYNTHETIC-CATALOG-KEY-CANARY-108"
CAPABILITIES = ["project_catalog", "explicit_project_create", "strict_connect"]
EPOCH = "0123456789abcdef0123456789abcdef"


def health(**changes):
    return dict(service="voxarium_studio_remote", version="test-v2", protocol=2,
                supported_protocols=[2], catalog_api=2, **changes)


def me():
    return {"protocol": 2, "catalog_api": 2, "capabilities": CAPABILITIES.copy(),
            "user": {"id": "user-1", "display_name": "Test User", "color": "#3e63dd"}}


def project(id="alpha", **changes):
    return {"id": id, "display_name": "Same name", "storage_epoch": EPOCH,
            "metadata_revision": 1, "created_at": "2026-11-15T12:00:00Z", **changes}


def response(body, status=200, headers=None):
    return CatalogResponse(status=status, body=deepcopy(body), headers=headers or {})


def envelope(p=None):
    return {"protocol": 2, "project": p or project()}


def page(*ids, next_after=None):
    return {"protocol": 2, "projects": [project(id) for id in ids], "next_after": next_after}


def failure(code, status):
    return response({"error": {"code": code, "message": KEY, "request_id": KEY}}, status)


class ScriptedTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": deepcopy(params), "json": deepcopy(json),
                           "follow_redirects": follow_redirects})
        assert follow_redirects is False
        assert self.replies, "Unexpected transport request (possibly a blind retry)"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


async def verified(*replies):
    transport = ScriptedTransport(response(health()), response(me()), *replies)
    client = CatalogClient(transport, url=BASE, token=KEY)
    await client.verify()
    return client, transport


async def test_verification_and_exact_prefixed_wire_auth():
    client, transport = await verified(response(page("alpha", "beta")),
                                       response(envelope()))
    result = await client.list_projects()
    assert isinstance(result.projects, tuple)
    assert [p.id for p in result.projects] == ["alpha", "beta"]
    assert result.projects[0].display_name == result.projects[1].display_name
    assert result.projects[0].space_instance_id == EPOCH
    assert result.next_after is None
    assert (await client.get_project("alpha")).id == "alpha"
    assert [(c["method"], c["url"]) for c in transport.calls] == [
        ("GET", BASE + "/healthz"), ("GET", BASE + "/api/v2/me"),
        ("GET", BASE + "/api/v2/projects"), ("GET", BASE + "/api/v2/projects/alpha")]
    assert "Authorization" not in transport.calls[0]["headers"]
    for call in transport.calls:
        assert KEY not in call["url"]
        assert KEY not in repr(call["params"])
        assert KEY not in repr(call["json"])
        assert call["json"] is None
    for call in transport.calls[1:]:
        assert call["headers"]["Authorization"] == "Bearer " + KEY
        assert "Cookie" not in call["headers"]
    assert transport.calls[2]["params"] == {"limit": 100}
    assert KEY not in repr(client)
    assert not hasattr(client, "select_project")


async def test_verify_returns_typed_user_and_capabilities():
    identity = me()
    identity["capabilities"] += ["project_snapshot", "legacy_resolution"]
    transport = ScriptedTransport(response(health()), response(identity))
    result = await CatalogClient(transport, url=BASE, token=KEY).verify()
    assert (result.user.id, result.user.display_name, result.user.color) == (
        "user-1", "Test User", "#3e63dd")
    assert result.capabilities == frozenset(identity["capabilities"])


@pytest.mark.parametrize("action", ["list", "get", "create"])
async def test_verification_is_required_before_catalog_calls(action):
    transport = ScriptedTransport()
    client = CatalogClient(transport, url=BASE, token=KEY)
    with pytest.raises(CatalogError) as caught:
        if action == "list":
            await client.list_projects()
        elif action == "get":
            await client.get_project("alpha")
        else:
            await client.create_project(id="alpha", display_name="Alpha", operation_id="op-1")
    assert caught.value.code == "verification_required"
    assert transport.calls == []


@pytest.mark.parametrize("field,value", [
    ("protocol", 1), ("protocol", 3), ("protocol", "2"), ("protocol", True),
    ("catalog_api", 1), ("catalog_api", "2"), ("supported_protocols", [1]),
    ("supported_protocols", "2"), ("service", None), ("version", None),
])
async def test_health_fails_closed_before_sending_key(field, value):
    body = health()
    body[field] = value
    transport = ScriptedTransport(response(body))
    with pytest.raises(CatalogError):
        await CatalogClient(transport, url=BASE, token=KEY).verify()
    assert len(transport.calls) == 1
    assert KEY not in repr(transport.calls)


@pytest.mark.parametrize("field", ["protocol", "catalog_api", "supported_protocols", "service", "version"])
async def test_missing_health_fields_fail_closed(field):
    body = health()
    del body[field]
    transport = ScriptedTransport(response(body))
    with pytest.raises(CatalogError):
        await CatalogClient(transport, url=BASE, token=KEY).verify()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("mutation", [
    {"protocol": 1}, {"protocol": "2"}, {"catalog_api": 3}, {"catalog_api": True},
    {"capabilities": []}, {"capabilities": "project_catalog"},
    {"capabilities": CAPABILITIES[:2]}, {"capabilities": CAPABILITIES + [None]},
    {"user": None}, {"user": {"id": "user-1", "name": "Old name", "color": "#3e63dd"}},
])
async def test_me_must_prove_v2_capabilities_and_user(mutation):
    body = me()
    body.update(mutation)
    transport = ScriptedTransport(response(health()), response(body))
    client = CatalogClient(transport, url=BASE, token=KEY)
    with pytest.raises(CatalogError):
        await client.verify()
    with pytest.raises(CatalogError) as caught:
        await client.list_projects()
    assert caught.value.code == "verification_required"
    assert len(transport.calls) == 2


@pytest.mark.parametrize("field", ["protocol", "catalog_api", "capabilities", "user"])
async def test_me_required_fields_cannot_be_omitted(field):
    body = me()
    del body[field]
    transport = ScriptedTransport(response(health()), response(body))
    with pytest.raises(CatalogError):
        await CatalogClient(transport, url=BASE, token=KEY).verify()


@pytest.mark.parametrize("id", ["", "Alpha", " alpha", "alpha ", "../alpha", "a/b", "%61", "а", "a" * 65, None, 1])
async def test_input_ids_are_exact_not_normalised(id):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.get_project(id)
    with pytest.raises(CatalogError):
        await client.create_project(id=id, display_name="Name", operation_id="op")
    assert len(transport.calls) == 2


@pytest.mark.parametrize("changes", [
    {"id": "Alpha"}, {"storage_epoch": "not-uuid"}, {"storage_epoch": EPOCH.upper()},
    {"metadata_revision": 0}, {"metadata_revision": True}, {"metadata_revision": "1"},
    {"created_at": None}, {"display_name": None},
])
async def test_project_metadata_is_validated(changes):
    client, _ = await verified(response(envelope(project(**changes))))
    with pytest.raises(CatalogError):
        await client.get_project("alpha")


@pytest.mark.parametrize("field", ["id", "display_name", "storage_epoch", "metadata_revision", "created_at"])
async def test_project_fields_cannot_be_omitted(field):
    body = project()
    del body[field]
    client, _ = await verified(response(envelope(body)))
    with pytest.raises(CatalogError):
        await client.get_project("alpha")


@pytest.mark.parametrize("body", [None, [], {}, {"protocol": 1, "project": project()},
    {"protocol": 2, "projects": [project()]}, envelope(project("beta")),
    {**envelope(), "extra": KEY}, envelope({**project(), "extra": KEY})])
async def test_get_requires_envelope_and_exact_requested_id(body):
    client, _ = await verified(response(body))
    with pytest.raises(CatalogError):
        await client.get_project("alpha")


async def test_pagination_uses_keyset_and_deduplicates_ids_not_names():
    client, transport = await verified(
        response(page("alpha", "beta", next_after="beta")), response(page("beta", "gamma")))
    result = await client.list_all(limit=2)
    assert isinstance(result, tuple)
    assert [p.id for p in result] == ["alpha", "beta", "gamma"]
    assert transport.calls[2]["params"] == {"limit": 2}
    assert transport.calls[3]["params"] == {"limit": 2, "after": "beta"}


@pytest.mark.parametrize("limit", [0, 201, True, "100", None])
async def test_invalid_page_limits_do_not_request(limit):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.list_projects(limit=limit)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("after", ["Alpha", " alpha", "a/b", "%61", "", 1])
async def test_invalid_input_cursor_does_not_request(after):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.list_projects(after=after)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("max_pages", [0, -1, True, "1"])
async def test_invalid_page_budget_does_not_request(max_pages):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.list_all(max_pages=max_pages)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("body", [
    {}, {"protocol": 2, "projects": []}, {"protocol": 2, "projects": {}, "next_after": None},
    page("alpha", next_after="../beta"), page("alpha", next_after="beta"),
    page("alpha", "alpha"), page("beta", "alpha"),
    {**page("alpha"), "extra": KEY},
])
async def test_malformed_page_or_cursor_is_rejected(body):
    client, _ = await verified(response(body))
    with pytest.raises(CatalogError):
        await client.list_projects()


async def test_nonadvancing_cursor_and_page_budget_are_bounded():
    client, transport = await verified(response(page("alpha", next_after="alpha")))
    with pytest.raises(CatalogError):
        await client.list_projects(after="alpha")
    assert len(transport.calls) == 3
    client, transport = await verified(response(page("alpha", next_after="alpha")))
    with pytest.raises(CatalogError):
        await client.list_all(max_pages=1)
    assert len(transport.calls) == 3


async def test_create_201_has_only_wire_body_and_never_replays():
    client, transport = await verified(response(envelope(), 201))
    result = await client.create_project(id="alpha", display_name="Same name", operation_id="op-1")
    assert result.state == "created"
    assert result.project.id == "alpha"
    assert (await client.create_project(id="alpha", display_name="Same name", operation_id="op-1")).state == "created"
    assert len(transport.calls) == 3
    assert transport.calls[-1]["method"] == "POST"
    assert transport.calls[-1]["json"] == {"id": "alpha", "display_name": "Same name"}
    assert transport.calls[-1]["url"] == BASE + "/api/v2/projects"
    assert "op-1" not in repr(transport.calls)
    with pytest.raises(CatalogError) as caught:
        await client.create_project(id="beta", display_name="Same name", operation_id="op-1")
    assert caught.value.code == "operation_conflict"
    with pytest.raises(CatalogError) as caught:
        await client.create_project(id="alpha", display_name="Other name", operation_id="op-1")
    assert caught.value.code == "operation_conflict"
    assert len(transport.calls) == 3


@pytest.mark.parametrize("reply,state", [
    (failure("project_exists", 409), "conflict"),
    (TimeoutError(KEY), "unknown"), (response({}, 201), "unknown"),
    (response(envelope(project("beta")), 201), "unknown"),
    (response({}, 307, {"Location": "https://other.invalid/" + KEY}), "unknown"),
    (failure("invalid_display_name", 400), "rejected"),
])
async def test_create_failure_outcomes_are_journaled_without_repost(reply, state, caplog):
    client, transport = await verified(reply)
    first = await client.create_project(id="alpha", display_name="Same name", operation_id="op")
    assert first.state == state
    assert first.project is None
    assert KEY not in repr(first)
    again = await client.create_project(id="alpha", display_name="Same name", operation_id="op")
    assert again.state == state
    assert len(transport.calls) == 3
    assert KEY not in caplog.text


@pytest.mark.parametrize("initial", [TimeoutError(KEY), failure("project_exists", 409)])
@pytest.mark.parametrize("recovery,state", [
    (response(envelope()), "found"), (failure("project_not_found", 404), "absent"),
    (TimeoutError(KEY), "unknown"), (response({}, 200), "unknown"),
    (response(envelope(project("beta"))), "unknown"),
])
async def test_recovery_only_gets_original_id_and_never_enables_replay(initial, recovery, state):
    client, transport = await verified(initial, recovery, recovery)
    await client.create_project(id="alpha", display_name="Same name", operation_id="op")
    for _ in range(2):
        result = await client.recover_create("op")
        assert result.state == state
        assert (result.project.id if result.project else None) == ("alpha" if state == "found" else None)
    replay = await client.create_project(id="alpha", display_name="Same name", operation_id="op")
    assert replay.state in {"unknown", "conflict", "found", "absent"}
    assert [c["method"] for c in transport.calls] == ["GET", "GET", "POST", "GET", "GET"]
    assert all(c["url"] == BASE + "/api/v2/projects/alpha" for c in transport.calls[3:])


async def test_new_explicit_operation_permits_another_create_after_absent():
    client, transport = await verified(TimeoutError(KEY), failure("project_not_found", 404), response(envelope(), 201))
    await client.create_project(id="alpha", display_name="Same name", operation_id="op-1")
    assert (await client.recover_create("op-1")).state == "absent"
    assert (await client.create_project(id="alpha", display_name="Same name", operation_id="op-2")).state == "created"
    assert [c["method"] for c in transport.calls].count("POST") == 2


@pytest.mark.parametrize("reply", [TimeoutError(KEY), failure(KEY, 400), failure("unauthorised", 401),
    response({"protocol": KEY}), response({}, 302, {"Location": "https://other.invalid/" + KEY})])
async def test_untrusted_errors_and_redirects_are_sanitised(reply, caplog):
    client, transport = await verified(reply)
    with pytest.raises(CatalogError) as caught:
        await client.get_project("alpha")
    error = caught.value
    assert isinstance(error.code, str) and error.code
    assert KEY not in str(error)
    assert KEY not in repr(error)
    assert KEY not in repr(vars(error))
    assert KEY not in caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None
    assert KEY not in "".join(traceback.format_exception(error))
    assert len(transport.calls) == 3


@pytest.mark.parametrize("code,status", [
    ("invalid_id", 400), ("invalid_display_name", 400), ("invalid_request", 400),
    ("unauthorised", 401), ("project_not_found", 404), ("metadata_conflict", 409),
    ("payload_too_large", 413), ("temporarily_unavailable", 503),
])
async def test_wire_errors_use_fixed_codes_not_remote_messages(code, status):
    client, _ = await verified(failure(code, status))
    with pytest.raises(CatalogError) as caught:
        await client.get_project("alpha")
    assert caught.value.code == code
    assert KEY not in repr(caught.value)


async def test_status_code_cannot_launder_an_unrelated_wire_error():
    client, _ = await verified(failure("project_not_found", 400))
    with pytest.raises(CatalogError) as caught:
        await client.get_project("alpha")
    assert caught.value.code != "project_not_found"


async def test_health_redirect_is_not_followed_and_never_receives_key():
    transport = ScriptedTransport(response({}, 308, {"Location": "https://other.invalid/"}))
    with pytest.raises(CatalogError):
        await CatalogClient(transport, url=BASE, token=KEY).verify()
    assert len(transport.calls) == 1
    assert "Authorization" not in transport.calls[0]["headers"]


class ObservedLock(asyncio.Lock):
    """Expose queued acquisition without sleeps or scheduler timing guesses."""

    def __init__(self, expected_waiters=1):
        super().__init__()
        self.expected_waiters = expected_waiters
        self.waiter_count = 0
        self.queued = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.waiter_count += 1
            if self.waiter_count == self.expected_waiters:
                self.queued.set()
        return await super().acquire()


async def test_waiting_create_rechecks_verification_at_mutation_boundary():
    client, transport = await verified(response(health() | {"protocol": 1}))
    lock = ObservedLock()
    client._create_lock = lock
    pending = None
    try:
        async with lock:
            pending = asyncio.create_task(client.create_project(
                id="alpha", display_name="Name", operation_id="queued-op"))
            await asyncio.wait_for(lock.queued.wait(), timeout=5)
            with pytest.raises(CatalogError):
                await client.verify()
        with pytest.raises(CatalogError) as caught:
            await pending
        assert caught.value.code == "verification_required"
        assert [call["method"] for call in transport.calls] == ["GET", "GET", "GET"]
    finally:
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_concurrent_same_operation_emits_exactly_one_post():
    client, transport = await verified(response(envelope(), 201))
    lock = ObservedLock(expected_waiters=2)
    client._create_lock = lock
    tasks = []
    try:
        async with lock:
            tasks = [asyncio.create_task(client.create_project(
                id="alpha", display_name="Same name", operation_id="same-op")) for _ in range(2)]
            await asyncio.wait_for(lock.queued.wait(), timeout=5)
        first, second = await asyncio.gather(*tasks)
        assert first == second
        assert first.state == "created"
        assert [call["method"] for call in transport.calls] == ["GET", "GET", "POST"]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancelled_transport_is_sanitised_and_create_recovers_only_by_get(caplog):
    client, transport = await verified(asyncio.CancelledError(KEY), response(envelope()))
    with pytest.raises(asyncio.CancelledError) as caught:
        await client.create_project(id="alpha", display_name="Same name", operation_id="cancel-op")
    error = caught.value
    assert error.args == ()
    assert error.__cause__ is None
    assert error.__context__ is None
    assert KEY not in str(error)
    assert KEY not in repr(error)
    assert KEY not in repr(vars(error))
    assert KEY not in caplog.text
    repeated = await client.create_project(id="alpha", display_name="Same name", operation_id="cancel-op")
    assert repeated.state == "unknown"
    assert repeated.project is None
    assert len(transport.calls) == 3
    recovered = await client.recover_create("cancel-op")
    assert recovered.state == "found"
    assert recovered.project.id == "alpha"
    await client.create_project(id="alpha", display_name="Same name", operation_id="cancel-op")
    assert [call["method"] for call in transport.calls] == ["GET", "GET", "POST", "GET"]
    assert transport.calls[-1]["url"] == BASE + "/api/v2/projects/alpha"


async def test_terminal_page_cannot_move_behind_requested_cursor():
    client, transport = await verified(response(page("alpha")))
    with pytest.raises(CatalogError):
        await client.list_projects(after="zeta")
    assert transport.calls[-1]["params"] == {"limit": 100, "after": "zeta"}


@pytest.mark.parametrize("url", [
    BASE + "/../escape", BASE + "/./escape", BASE + "/%2fescape", BASE + "/%5cescape",
    BASE + "/%252fescape", "https://" + KEY + "@catalog.invalid/team",
    "https://catalog.invalid/team?token=" + KEY,
    "https://catalog.invalid/team#" + KEY,
])
async def test_bad_endpoint_prefixes_never_reach_transport_or_echo_credentials(url, caplog):
    transport = ScriptedTransport()
    with pytest.raises(CatalogError) as caught:
        CatalogClient(transport, url=url, token=KEY)
    assert KEY not in str(caught.value)
    assert KEY not in repr(caught.value)
    assert KEY not in repr(vars(caught.value))
    assert KEY not in caplog.text
    assert transport.calls == []


@pytest.mark.parametrize("label", ["", "   ", None, 1, "a" * 121, "a\nb", "a\x7fb", "a\x85b", "a\u2028b", "a\u2029b", "a\u202eb", "a\u2066b"])
async def test_invalid_create_labels_do_not_post(label):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.create_project(id="alpha", display_name=label, operation_id="op")
    assert len(transport.calls) == 2


async def test_create_label_is_explicitly_trimmed_and_nfc_normalised():
    client, transport = await verified(response(envelope(project(display_name="Café")), 201))
    result = await client.create_project(id="alpha", display_name="  Cafe\u0301  ", operation_id="op")
    assert result.state == "created"
    assert transport.calls[-1]["json"] == {"id": "alpha", "display_name": "Café"}


@pytest.mark.parametrize("operation_id", ["", None, 1, "op/one", "op one", "x" * 129])
async def test_invalid_operation_ids_do_not_post(operation_id):
    client, transport = await verified()
    with pytest.raises(CatalogError):
        await client.create_project(id="alpha", display_name="Name", operation_id=operation_id)
    assert len(transport.calls) == 2


async def test_older_verify_cannot_restore_verification_after_newer_failed_probe():
    entered, release = asyncio.Event(), asyncio.Event()

    class DeferredMe(ScriptedTransport):
        async def request(self, *args, **kwargs):
            result = await super().request(*args, **kwargs)
            if len(self.calls) == 4:
                entered.set()
                await release.wait()
            return result

    transport = DeferredMe(response(health()), response(me()), response(health()),
                           response(me()), response(health() | {"protocol": 1}))
    client = CatalogClient(transport, url=BASE, token=KEY)
    await client.verify()
    pending = asyncio.create_task(client.verify())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(CatalogError, match="version_mismatch"):
            await client.verify()
        release.set()
        with pytest.raises(CatalogError, match="verification_stale"):
            await pending
        with pytest.raises(CatalogError, match="verification_required"):
            await client.create_project(id="alpha", display_name="Name", operation_id="op")
        assert all(c["method"] == "GET" for c in transport.calls)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("during_me", [False, True])
async def test_cancelled_verification_never_leaks_message_or_enables_catalog(during_me, caplog):
    replies = [response(health()), asyncio.CancelledError(KEY)] if during_me else [asyncio.CancelledError(KEY)]
    transport = ScriptedTransport(*replies)
    client = CatalogClient(transport, url=BASE, token=KEY)
    with pytest.raises(asyncio.CancelledError) as caught:
        await client.verify()
    assert caught.value.args == ()
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert KEY not in caplog.text
    with pytest.raises(CatalogError, match="verification_required"):
        await client.list_projects()
    assert len(transport.calls) == (2 if during_me else 1)


@pytest.mark.parametrize("reply", [response(envelope() | {"protocol": 1}), failure("unauthorised", 401)])
async def test_version_or_auth_failure_blocks_subsequent_mutation(reply):
    client, transport = await verified(reply)
    with pytest.raises(CatalogError):
        await client.get_project("alpha")
    with pytest.raises(CatalogError, match="verification_required"):
        await client.create_project(id="alpha", display_name="Name", operation_id="op")
    assert all(c["method"] == "GET" for c in transport.calls)

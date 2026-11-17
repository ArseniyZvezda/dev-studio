"""Offline snapshot/legacy contracts (server-design §3.1, §3.3–3.4).

Only synthetic decoded responses cross the injected fake-transport boundary.
No HTTP client, filesystem project, DNS, server or live sync is involved.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
import traceback

import pytest

from voxarium_studio.sync.catalog import CatalogError, CatalogResponse
from voxarium_studio.sync.catalog_reads import CatalogReads, LegacyResolution, ProjectSnapshot


BASE = "https://catalog.invalid/deployment/team"
CANARY = "SYNTHETIC-READS-SECRET-CANARY-109"
EPOCH = "0123456789abcdef0123456789abcdef"
OTHER_EPOCH = "f" * 32
BASE_CAPS = ["project_catalog", "explicit_project_create", "strict_connect"]
CAPS = BASE_CAPS + ["project_snapshot", "legacy_resolution"]


def health():
    return {"service": "voxarium_studio_remote", "version": "synthetic-v2", "protocol": 2,
            "supported_protocols": [2], "catalog_api": 2}


def me(caps=None):
    return {"protocol": 2, "catalog_api": 2, "capabilities": CAPS.copy() if caps is None else caps,
            "user": {"id": "user-1", "display_name": "Test User", "color": "#3e63dd"}}


def project(**changes):
    return {"id": "alpha", "display_name": "Same label", "storage_epoch": EPOCH,
            "metadata_revision": 1, "created_at": "2026-11-15T12:00:00Z", **changes}


def document(path="a.md", content="Synthetic текст", **changes):
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest() if isinstance(content, str) else "0" * 64
    return {"path": path, "content": content,
            "hash": digest, "rev": 1,
            "deleted": False, "updated_by": "user-1", "updated_at": "2026-11-15T12:00:00Z",
            **changes}


def page(*docs, next_after=None, **snapshot_changes):
    return {"protocol": 2, "snapshot": {"id": "alpha", "storage_epoch": EPOCH,
            "seq": 4, **snapshot_changes}, "documents": list(docs), "next_after": next_after}


def legacy(name="Old Name", **project_changes):
    return {"protocol": 2, "legacy_space_name": name, "project": project(**project_changes)}


def response(body, status=200, headers=None):
    return CatalogResponse(status=status, body=deepcopy(body), headers=headers or {})


def failure(code, status):
    return response({"error": {"code": code, "message": CANARY, "request_id": CANARY}}, status)


class ScriptedTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def request(self, method, url, *, headers, params, json, follow_redirects):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "params": deepcopy(params), "json": deepcopy(json),
                           "follow_redirects": follow_redirects})
        assert method == "GET", "Read adapter attempted a mutation"
        assert follow_redirects is False
        assert self.replies, "Unexpected request / blind retry"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


async def verified(*replies, caps=None):
    transport = ScriptedTransport(response(health()), response(me(caps)), *replies)
    reads = CatalogReads(transport, url=BASE, token=CANARY)
    await reads.verify()
    return reads, transport


def assert_safe(error, caplog):
    assert CANARY not in str(error)
    assert CANARY not in repr(error)
    assert CANARY not in repr(vars(error))
    assert CANARY not in "".join(traceback.format_exception(error))
    assert CANARY not in caplog.text
    assert error.__cause__ is None
    assert error.__context__ is None


async def test_complete_snapshot_is_frozen_and_pins_all_continuations():
    tombstone = document("b.txt", content=None, hash=None, deleted=True, rev=4, updated_by=None)
    reads, transport = await verified(
        response(page(document(), next_after="a.md")), response(page(tombstone)))
    result = await reads.snapshot("alpha", expected_epoch=EPOCH, limit=1)
    assert isinstance(result, ProjectSnapshot)
    assert (result.id, result.storage_epoch, result.space_instance_id, result.seq) == (
        "alpha", EPOCH, EPOCH, 4)
    assert isinstance(result.documents, tuple)
    assert [d.path for d in result.documents] == ["a.md", "b.txt"]
    assert result.documents[0].content == "Synthetic текст"
    assert result.documents[1].deleted is True
    assert result.documents[1].content is result.documents[1].hash is None
    with pytest.raises(FrozenInstanceError):
        result.seq = 99
    with pytest.raises(FrozenInstanceError):
        result.documents[0].content = "overwrite"
    assert transport.calls[2]["params"] == {"limit": 1, "expected_epoch": EPOCH}
    assert transport.calls[3]["params"] == {
        "limit": 1, "after": "a.md", "expected_seq": 4, "expected_epoch": EPOCH}
    assert [c["url"] for c in transport.calls] == [BASE + "/healthz", BASE + "/api/v2/me",
        BASE + "/api/v2/projects/alpha/snapshot", BASE + "/api/v2/projects/alpha/snapshot"]
    assert "Authorization" not in transport.calls[0]["headers"]
    for call in transport.calls[1:]:
        assert call["headers"]["Authorization"] == "Bearer " + CANARY
        assert "Cookie" not in call["headers"]
        assert call["json"] is None
        assert CANARY not in call["url"] + repr(call["params"])
    assert CANARY not in repr(reads) + repr(result)


async def test_empty_snapshot_has_zero_seq_and_default_limit_without_expected_epoch():
    reads, transport = await verified(response(page(seq=0)))
    result = await reads.snapshot("alpha")
    assert result.documents == ()
    assert result.seq == 0
    assert transport.calls[-1]["params"] == {"limit": 20}


@pytest.mark.parametrize("action", ["snapshot", "legacy"])
async def test_verification_required_before_any_read(action):
    transport = ScriptedTransport()
    reads = CatalogReads(transport, url=BASE, token=CANARY)
    with pytest.raises(CatalogError, match="verification_required"):
        if action == "snapshot":
            await reads.snapshot("alpha")
        else:
            await reads.resolve_legacy("Old Name")
    assert transport.calls == []


@pytest.mark.parametrize("cap,action", [("project_snapshot", "snapshot"), ("legacy_resolution", "legacy")])
async def test_missing_flow_capability_does_not_probe_guessed_endpoint(cap, action):
    reads, transport = await verified(caps=[c for c in CAPS if c != cap])
    with pytest.raises(CatalogError, match="capabilities_missing"):
        if action == "snapshot":
            await reads.snapshot("alpha")
        else:
            await reads.resolve_legacy("Old Name")
    assert len(transport.calls) == 2


@pytest.mark.parametrize("missing", BASE_CAPS)
async def test_verify_keeps_original_base_capability_requirements(missing):
    transport = ScriptedTransport(response(health()), response(me([c for c in CAPS if c != missing])))
    reads = CatalogReads(transport, url=BASE, token=CANARY)
    with pytest.raises(CatalogError, match="capabilities_missing"):
        await reads.verify()
    assert len(transport.calls) == 2


@pytest.mark.parametrize("id", ["Alpha", " alpha", "alpha ", "../alpha", "a/b", "%61", "а", "a" * 65, "", None, 1])
async def test_snapshot_id_is_exact_and_never_normalised(id):
    reads, transport = await verified()
    with pytest.raises(CatalogError):
        await reads.snapshot(id)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("kwargs", [
    {"limit": v} for v in [0, 101, True, "20", None, 1.0]
] + [{"max_pages": v} for v in [0, -1, True, "1", None, 1.0]]
  + [{"max_bytes": v} for v in [0, -1, True, "1", None, 1.0]]
  + [{"expected_epoch": v} for v in [EPOCH.upper(), "0" * 31, "0" * 33, "g" * 32, " " + EPOCH, 1, True, ""]])
async def test_invalid_snapshot_parameters_fail_before_request(kwargs):
    reads, transport = await verified()
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha", **kwargs)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("body", [None, [], {}, {**page(), "extra": CANARY},
    {k: v for k, v in page().items() if k != "next_after"},
    {**page(), "protocol": True}, {**page(), "protocol": 1},
    {**page(), "documents": {}}, {**page(), "documents": None},
    {**page(), "snapshot": {}}, {**page(), "snapshot": {**page()["snapshot"], "extra": CANARY}},
    page(id="beta"), page(id="Alpha"), page(storage_epoch=EPOCH.upper()),
    page(storage_epoch=""), page(storage_epoch=None), page(seq=-1), page(seq=True), page(seq="4")])
async def test_snapshot_envelope_and_identity_are_strict(body):
    reads, transport = await verified(response(body))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")
    assert len(transport.calls) == 3


@pytest.mark.parametrize("field", ["path", "content", "hash", "rev", "deleted", "updated_by", "updated_at"])
async def test_document_fields_cannot_be_omitted(field):
    doc = document()
    del doc[field]
    reads, _ = await verified(response(page(doc)))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")


@pytest.mark.parametrize("changes", [
    {"extra": CANARY}, {"deleted": 0}, {"deleted": "false"}, {"deleted": None},
    {"content": None}, {"content": 1}, {"hash": None}, {"hash": "0" * 64},
    {"hash": document()["hash"].upper()}, {"rev": 0}, {"rev": 5}, {"rev": True}, {"rev": "1"},
    {"updated_by": ""}, {"updated_by": 1}, {"updated_at": ""}, {"updated_at": None},
    {"deleted": True}, {"deleted": True, "content": None}, {"deleted": True, "hash": None},
])
async def test_document_schema_hash_and_revision_fail_closed(changes):
    reads, _ = await verified(response(page(document(**changes))))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")


@pytest.mark.parametrize("path", ["", " a.md", "a.md ", "/a.md", "../a.md", "a/../b.md", "./a.md",
    "a//b.md", "a\\b.md", "a\x00.md", "a\n.md", "a.pdf", "a" * 510 + ".md", None, 1])
async def test_paths_are_exact_safe_document_paths(path):
    reads, _ = await verified(response(page(document(path))))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")


async def test_binary_utf8_path_order_and_supported_suffixes():
    paths = ["A.md", "a.markdown", "z.txt", "é.md", "я.md"]
    assert paths == sorted(paths, key=lambda p: p.encode("utf-8"))
    reads, _ = await verified(response(page(*(document(p) for p in paths))))
    assert [d.path for d in (await reads.snapshot("alpha")).documents] == paths


@pytest.mark.parametrize("body", [
    page(document("b.md"), document("a.md")), page(document(), document()),
    page(next_after="a.md"), page(document(), next_after="b.md"),
    page(document(), next_after="../a.md"), page(document(), next_after=""),
    page(document(), next_after=1),
])
async def test_invalid_order_or_cursor_cannot_produce_success(body):
    reads, transport = await verified(response(body))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")
    assert len(transport.calls) == 3


@pytest.mark.parametrize("second", [page(document()), page(document("0.md")),
    page(next_after="a.md"), page(document("b.md"), seq=5),
    page(document("b.md"), storage_epoch=OTHER_EPOCH), page(document("b.md"), id="beta")])
async def test_continuation_never_mixes_epochs_sequences_ids_or_duplicate_paths(second):
    reads, transport = await verified(response(page(document(), next_after="a.md")), response(second))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")
    assert len(transport.calls) == 4


async def test_expected_epoch_checked_on_first_response():
    reads, transport = await verified(response(page(storage_epoch=OTHER_EPOCH)))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha", expected_epoch=EPOCH)
    assert len(transport.calls) == 3


async def test_page_budget_is_bounded_without_returning_partial_result():
    reads, transport = await verified(response(page(document(), next_after="a.md")))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha", max_pages=1)
    assert len(transport.calls) == 3


async def test_page_cannot_exceed_requested_document_limit():
    reads, _ = await verified(response(page(document(), document("b.md"))))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha", limit=1)


async def test_utf8_content_byte_limit_not_character_limit():
    reads, _ = await verified(response(page(document(content="я" * 500_001))))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")
    reads, _ = await verified(response(page(document(content="я" * 500_000))))
    assert len((await reads.snapshot("alpha")).documents[0].content) == 500_000


async def test_compact_utf8_json_page_size_and_aggregate_budget():
    body = page(*(document(f"{i}.md", content="я" * 450_000) for i in range(5)))
    assert len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 4 * 1024 * 1024
    reads, _ = await verified(response(body))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha")
    first = page(document(), next_after="a.md")
    second = page(document("b.md"))
    budget = len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    reads, transport = await verified(response(first), response(second))
    with pytest.raises(CatalogError):
        await reads.snapshot("alpha", max_bytes=budget)
    assert len(transport.calls) == 4


@pytest.mark.parametrize("name", [" Old Name ", "MiXeD", "Проект Ё", "Cafe\u0301", "alpha", "a/b?x=1&name=other"])
async def test_legacy_exact_selector_preserved_without_id_normalisation(name):
    reads, transport = await verified(response(legacy(name)))
    result = await reads.resolve_legacy(name)
    assert isinstance(result, LegacyResolution)
    assert result.legacy_space_name == name
    assert result.project.id == "alpha"
    assert result.project.space_instance_id == EPOCH
    with pytest.raises(FrozenInstanceError):
        result.legacy_space_name = "other"
    assert transport.calls[-1]["url"] == BASE + "/api/v2/legacy-spaces/resolve"
    assert transport.calls[-1]["params"] == {"name": name}
    assert transport.calls[-1]["json"] is None


@pytest.mark.parametrize("name", [None, 1, True, "", "a\nb", "a\x00b", "a\x7fb", "a\x85b", "x" * 10000])
async def test_invalid_legacy_selector_never_requests(name):
    reads, transport = await verified()
    with pytest.raises(CatalogError):
        await reads.resolve_legacy(name)
    assert len(transport.calls) == 2


@pytest.mark.parametrize("body", [None, {}, [], {**legacy(), "extra": CANARY},
    {k: v for k, v in legacy().items() if k != "legacy_space_name"},
    {**legacy(), "protocol": 1}, {**legacy(), "protocol": True},
    legacy("old name"), legacy(" Old Name "), {**legacy(), "legacy_space_name": None},
    legacy(id="Alpha"), legacy(storage_epoch=EPOCH.upper()), legacy(metadata_revision=True),
    {**legacy(), "project": {**project(), "extra": CANARY}},
])
async def test_legacy_exact_envelope_echo_and_project_metadata(body):
    reads, _ = await verified(response(body))
    with pytest.raises(CatalogError):
        await reads.resolve_legacy("Old Name")


@pytest.mark.parametrize("action,code,status", [
    ("snapshot", "snapshot_changed", 409), ("snapshot", "project_not_found", 404),
    ("legacy", "legacy_space_not_found", 404), ("snapshot", "unauthorised", 401),
    ("legacy", "temporarily_unavailable", 503),
])
async def test_fixed_wire_errors_preserve_code_without_echo(action, code, status, caplog):
    reads, transport = await verified(failure(code, status))
    with pytest.raises(CatalogError) as caught:
        if action == "snapshot":
            await reads.snapshot("alpha")
        else:
            await reads.resolve_legacy("Old Name")
    assert caught.value.code == code
    assert_safe(caught.value, caplog)
    assert len(transport.calls) == 3


@pytest.mark.parametrize("reply", [failure("snapshot_changed", 409), TimeoutError(CANARY),
    response({"protocol": CANARY}), failure(CANARY, 400),
    response({}, 307, {"Location": "https://other.invalid/" + CANARY})])
async def test_second_page_failure_discards_partial_preview_without_retry(reply, caplog):
    reads, transport = await verified(response(page(document(), next_after="a.md")), reply,
                                      response(page(seq=0)))
    with pytest.raises(CatalogError) as caught:
        await reads.snapshot("alpha")
    assert_safe(caught.value, caplog)
    assert len(transport.calls) == 4
    # A new explicit request must begin at the start, never resume hidden partial data.
    if not (isinstance(reply, CatalogResponse) and isinstance(reply.body, dict)
            and "protocol" in reply.body):
        assert (await reads.snapshot("alpha")).documents == ()
        assert transport.calls[-1]["params"] == {"limit": 20}


@pytest.mark.parametrize("reply", [TimeoutError(CANARY), failure(CANARY, 400),
    response({"protocol": CANARY}), response({}, 302, {"Location": "https://other.invalid/" + CANARY}),
    failure("legacy_space_not_found", 400)])
async def test_legacy_untrusted_failure_is_sanitised_without_fallback(reply, caplog):
    reads, transport = await verified(reply)
    with pytest.raises(CatalogError) as caught:
        await reads.resolve_legacy("Old Name")
    assert_safe(caught.value, caplog)
    if isinstance(reply, CatalogResponse) and reply.status == 400:
        assert caught.value.code != "legacy_space_not_found"
    assert len(transport.calls) == 3


@pytest.mark.parametrize("action", ["snapshot", "legacy"])
async def test_cancelled_transport_retains_cancellation_not_secret(action, caplog):
    reads, transport = await verified(asyncio.CancelledError(CANARY))
    with pytest.raises(asyncio.CancelledError) as caught:
        if action == "snapshot":
            await reads.snapshot("alpha")
        else:
            await reads.resolve_legacy("Old Name")
    assert caught.value.args == ()
    assert_safe(caught.value, caplog)
    assert len(transport.calls) == 3


@pytest.mark.parametrize("action", ["snapshot", "legacy"])
@pytest.mark.parametrize("successful_verify", [False, True])
async def test_reverification_fences_inflight_read(action, successful_verify):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedTransport(ScriptedTransport):
        async def request(self, method, url, **kwargs):
            reply = await super().request(method, url, **kwargs)
            if url.endswith(("/snapshot", "/resolve")):
                entered.set()
                await release.wait()
            return reply

    reprobe_health = health()
    if not successful_verify:
        reprobe_health["protocol"] = 1
    transport = PausedTransport(response(health()), response(me()),
        response(page() if action == "snapshot" else legacy()),
        response(reprobe_health), response(me()))
    reads = CatalogReads(transport, url=BASE, token=CANARY)
    await reads.verify()
    pending = asyncio.create_task(reads.snapshot("alpha") if action == "snapshot"
                                  else reads.resolve_legacy("Old Name"))
    await entered.wait()
    if successful_verify:
        await reads.verify()
    else:
        with pytest.raises(CatalogError):
            await reads.verify()
    release.set()
    with pytest.raises(CatalogError):
        await pending


@pytest.mark.parametrize("action", ["snapshot", "legacy"])
async def test_auth_failure_revokes_read_permission_without_probing_again(action):
    reads, transport = await verified(failure("unauthorised", 401))
    with pytest.raises(CatalogError, match="unauthorised"):
        if action == "snapshot":
            await reads.snapshot("alpha")
        else:
            await reads.resolve_legacy("Old Name")
    with pytest.raises(CatalogError, match="verification_required"):
        await reads.snapshot("alpha")
    with pytest.raises(CatalogError, match="verification_required"):
        await reads.resolve_legacy("Old Name")
    assert len(transport.calls) == 3


@pytest.mark.parametrize("reply", [failure("snapshot_changed", 409),
                                 TimeoutError(CANARY), asyncio.CancelledError(CANARY)])
async def test_failed_later_snapshot_cannot_touch_previous_complete_result(reply):
    reads, transport = await verified(response(page(document())),
        response(page(document("b.md"), next_after="b.md")), reply)
    previous = await reads.snapshot("alpha")
    with pytest.raises((CatalogError, asyncio.CancelledError)):
        await reads.snapshot("alpha")
    assert tuple(d.path for d in previous.documents) == ("a.md",)
    assert previous.documents[0].content == "Synthetic текст"
    assert len(transport.calls) == 5
    assert not any(hasattr(reads, name) for name in (
        "create_project", "select_project", "enable", "_snapshot", "documents"))


@pytest.mark.parametrize("field", ["path", "content", "hash", "updated_by", "updated_at"])
async def test_malformed_unicode_is_redacted_without_exception_context(field, caplog):
    doc = document()
    doc[field] = "\ud800" + CANARY
    reads, _ = await verified(response(page(doc)))
    with pytest.raises(CatalogError) as caught:
        await reads.snapshot("alpha")
    assert_safe(caught.value, caplog)


@pytest.mark.parametrize("name", ["\ud800" + CANARY])
async def test_malformed_legacy_unicode_is_redacted_before_transport(name, caplog):
    reads, transport = await verified()
    with pytest.raises(CatalogError) as caught:
        await reads.resolve_legacy(name)
    assert_safe(caught.value, caplog)
    assert len(transport.calls) == 2


async def test_short_page_from_byte_budget_is_not_mistaken_for_final_page():
    reads, transport = await verified(response(page(document(), next_after="a.md")),
                                      response(page(document("b.md"))))
    assert len((await reads.snapshot("alpha", limit=100)).documents) == 2
    assert transport.calls[-1]["params"]["after"] == "a.md"
    assert transport.calls[-1]["params"]["expected_epoch"] == EPOCH


async def test_exact_aggregate_byte_budget_accepts_complete_result():
    first = page(document(), next_after="a.md")
    second = page(document("b.md"))
    budget = sum(len(json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                 for p in (first, second))
    reads, _ = await verified(response(first), response(second))
    assert len((await reads.snapshot("alpha", max_bytes=budget)).documents) == 2

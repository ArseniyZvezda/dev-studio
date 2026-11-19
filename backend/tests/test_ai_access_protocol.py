"""AI Control Protocol 1 pure contracts (ai-access-design §5–§8, dev spec §16.12).

No network, server or AI: canonical JSON, strict parsing, schemas, write paths,
permissions, frames, capabilities discovery classification and the admission clock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from voxarium_studio.ai_access import protocol as p
from voxarium_studio.ai_access.protocol import Operation, Permission, ProtocolError

BACKEND = Path(__file__).resolve().parents[1]


def uid() -> str:
    return str(uuid.uuid4())


def target(**overrides) -> dict:
    data = {
        "server_instance_id": uid(), "executor_registration_id": uid(), "export_id": uid(),
        "persistent_id": "demo-project", "storage_epoch": "a" * 32, "export_revision": 1,
    }
    data.update(overrides)
    return data


def grant_data(**overrides) -> dict:
    data = {
        "grant_id": uid(), "revision": 1, "target": target(),
        "grantee_user_id": uid(), "grantee_registration_id": uid(),
        "operations": ["view_status", "run_task"], "team_ids": ["team-a"],
        "member_ids": ["dev"], "execution_policy_digest": "b" * 64,
        "issued_at": 1_000, "expires_at": 1_000 + p.GRANT_DEFAULT_MS,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- canonical JSON


def test_canonical_json_is_sorted_compact_utf8() -> None:
    assert p.canonical_json({"b": 1, "a": [True, None, "é"]}) == (
        '{"a":[true,null,"é"],"b":1}'.encode()
    )


def test_canonical_json_rejects_floats_and_non_json() -> None:
    for value in ({"a": 1.0}, [float("nan")], {"a": object()}, {1: "x"}):
        with pytest.raises(ProtocolError):
            p.canonical_json(value)


def test_canonical_hash_does_not_normalise_text_and_keeps_bool_distinct() -> None:
    nfc = unicodedata.normalize("NFC", "é")
    nfd = unicodedata.normalize("NFD", "é")
    assert nfc != nfd
    assert p.canonical_hash({"t": nfc}) != p.canonical_hash({"t": nfd})
    assert p.canonical_hash({"a": True}) != p.canonical_hash({"a": 1})
    assert p.canonical_hash({"b": 2, "a": 1}) == p.canonical_hash({"a": 1, "b": 2})


def test_record_hash_excludes_only_hash() -> None:
    record = {"export_id": "x", "revision": 2, "hash": "ignored"}
    assert p.record_hash(record) == p.canonical_hash({"export_id": "x", "revision": 2})


# --------------------------------------------------------------------------- strict parse


@pytest.mark.parametrize(("data", "code"), [
    ('{"a":1,"a":2}', "duplicate_key"),
    ('{"a":1.5}', "float_not_allowed"),
    ('{"a":1e3}', "float_not_allowed"),
    ('{"a":NaN}', "float_not_allowed"),
    ('{"a":Infinity}', "float_not_allowed"),
    ("[" * 13 + "]" * 13, "too_deep"),
    (b'"\xff"', "invalid_utf8"),
    (b"\xef\xbb\xbf{}", "invalid_json"),
    ("{", "invalid_json"),
    ('{"a":%d}' % (2**63), "integer_out_of_range"),
])
def test_parse_json_rejects(data: str | bytes, code: str) -> None:
    with pytest.raises(ProtocolError) as caught:
        p.parse_json(data)
    assert caught.value.code == code


def test_parse_json_limits_depth_fields_and_size() -> None:
    assert p.parse_json("[" * 12 + "]" * 12) is not None
    fields = {f"k{i}": i for i in range(p.MAX_OBJECT_FIELDS)}
    assert p.parse_json(json.dumps(fields)) == fields
    fields["one_more"] = 0
    with pytest.raises(ProtocolError) as caught:
        p.parse_json(json.dumps(fields))
    assert caught.value.code == "too_many_fields"
    with pytest.raises(ProtocolError) as caught:
        p.parse_json('"' + "a" * p.MAX_FRAME_BYTES + '"')
    assert caught.value.code == "frame_too_large"


# --------------------------------------------------------------------------- strict models


def test_strict_models_reject_unknown_fields_bool_ints_and_non_canonical_uuid() -> None:
    assert p.Target.from_wire(target()).export_revision == 1
    with pytest.raises(ValidationError):
        p.Target.from_wire({**target(), "extra": 1})
    with pytest.raises(ValidationError):
        p.Target.from_wire(target(export_revision=True))
    with pytest.raises(ValidationError):
        p.Target.from_wire(target(export_revision=0))
    with pytest.raises(ValidationError):
        p.Target.from_wire(target(export_id=uid().upper()))
    with pytest.raises(ValidationError):
        p.Target.from_wire(target(export_id=uuid.uuid4().hex))
    with pytest.raises(ValidationError):
        p.Target.from_wire(target(persistent_id="Bad ID"))


def test_validation_errors_do_not_echo_input() -> None:
    canary = "canary-secret-7f3a"
    with pytest.raises(ValidationError) as caught:
        p.Target.from_wire(target(export_id=canary))
    assert canary not in str(caught.value)


# --------------------------------------------------------------------------- write paths


@pytest.mark.parametrize("value", [
    "/etc/passwd", "C:/Windows", "C:", "c:relative", "\\\\srv\\share", "//srv/share",
    "a\\b", "../x", "a/../b", "a/./b", "a//b", "./a", "a/*/b", "*", "a*", "a?",
    "dir/", "", "a\x00b", "x" * 257,
])
def test_write_path_rejects(value: str) -> None:
    with pytest.raises(ProtocolError):
        p.check_write_path(value)


@pytest.mark.parametrize("value", ["src/main.py", "dir/*", "docs/a b.md", "x" * 256])
def test_write_path_accepts(value: str) -> None:
    assert p.check_write_path(value) == value


def test_write_paths_list_limits() -> None:
    assert p.check_write_paths([f"f{i}" for i in range(64)])
    with pytest.raises(ProtocolError):
        p.check_write_paths([f"f{i}" for i in range(65)])
    with pytest.raises(ProtocolError):
        p.check_write_paths(["a", "a"])
    with pytest.raises(ProtocolError):
        p.check_write_paths("a")


def test_resolve_write_path_refuses_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "inside").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert p.resolve_write_path(root, "inside/file.txt").parent == (root / "inside").resolve()
    assert p.resolve_write_path(root, "inside/*") == (root / "inside").resolve()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("this OS account may not create symbolic links")
        import _winapi  # a junction needs no symlink privilege and escapes the same way

        _winapi.CreateJunction(str(outside), str(root / "link"))
    with pytest.raises(ProtocolError):
        p.resolve_write_path(root, "link/file.txt")


# --------------------------------------------------------------------------- requests


def request(op: str, **extra) -> dict:
    data = {
        "target": target(), "expected_executor_epoch": uid(), "grant_id": uid(),
        "grant_revision": 1, "op": op, "payload": {},
    }
    data.update(extra)
    return data


def test_request_body_mutation_shape() -> None:
    read = p.RequestBody.from_wire(request("catalog.list"))
    assert read.parsed_payload() == p.CatalogListPayload()
    with pytest.raises(ValidationError):
        p.RequestBody.from_wire(request("catalog.list", command_id=uid(), issued_at=1))
    with pytest.raises(ValidationError):  # mutation without command_id/issued_at
        p.RequestBody.from_wire(request("task.start", expected_resource_revision=1))
    with pytest.raises(ValidationError):  # start needs expected_resource_revision
        p.RequestBody.from_wire(request("task.start", command_id=uid(), issued_at=1))
    start = p.RequestBody.from_wire(request(
        "task.start", command_id=uid(), issued_at=1, expected_resource_revision=3,
        payload={"resource_id": "task_1"},
    ))
    assert start.parsed_payload() == p.TaskStartPayload(resource_id="task_1")
    with pytest.raises(ValidationError):  # cancel needs expected_run_id
        p.RequestBody.from_wire(request("task.cancel", command_id=uid(), issued_at=1))
    with pytest.raises(ValidationError):  # actor fields are not part of the body
        p.RequestBody.from_wire(request("catalog.list", actor_id="director"))
    bad = p.RequestBody.from_wire(request(
        "task.note", command_id=uid(), issued_at=1, payload={"resource_id": "t", "x": 1},
    ))
    with pytest.raises(ValidationError):
        bad.parsed_payload()


# --------------------------------------------------------------------------- permissions


def test_required_permissions_never_imply_derived_rights() -> None:
    start = p.required_permissions(Operation.TASK_START)
    assert start == {Permission.RUN_TASK, Permission.VIEW_STATUS}
    assert Permission.CREATE_TASK not in start and Permission.VIEW_TRANSCRIPT not in start
    assert p.required_permissions(Operation.TASK_START, existing_foreign=True) == {
        Permission.RUN_TASK, Permission.VIEW_STATUS, Permission.OPERATE_EXISTING,
    }
    # Creating is never an operation on an existing object.
    assert Permission.OPERATE_EXISTING not in p.required_permissions(
        Operation.TASK_CREATE, existing_foreign=True)
    # Reads never need operate_existing.
    assert p.required_permissions(Operation.RESOURCE_GET, existing_foreign=True) == {
        Permission.VIEW_STATUS}
    assert p.required_permissions(Operation.TRANSCRIPT_PAGE) == {
        Permission.VIEW_STATUS, Permission.VIEW_TRANSCRIPT}
    assert p.required_permissions(Operation.STREAM_SUBSCRIBE, topic="status") == {
        Permission.VIEW_STATUS}
    assert p.required_permissions(Operation.STREAM_SUBSCRIBE, topic="transcript") == {
        Permission.VIEW_STATUS, Permission.VIEW_TRANSCRIPT}


# --------------------------------------------------------------------------- grants


def test_grant_validation() -> None:
    grant = p.Grant.from_wire(grant_data())
    assert grant.resource_scope is p.ResourceScope.CREATED_BY_THIS_GRANTEE
    assert not grant.expired(grant.expires_at - 1) and grant.expired(grant.expires_at)
    p.Grant.from_wire(grant_data(expires_at=1_000 + p.GRANT_MAX_MS))
    for bad in (
        grant_data(expires_at=1_000 + p.GRANT_MAX_MS + 1),
        grant_data(expires_at=1_000),
        grant_data(operations=["run_task"]),
        grant_data(operations=["view_transcript"]),
        grant_data(operations=["view_status", "operate_existing"]),
        grant_data(resource_scope="selected_resources"),
        grant_data(resource_ids=["task_1"]),
        grant_data(team_ids=[]),
        grant_data(member_ids=[]),
        grant_data(grantee_user_id="*"),
        grant_data(operations=["view_status", "view_status"]),
        grant_data(operations=["view_status", "admin"]),
        grant_data(revoked_at=999),
    ):
        with pytest.raises(ValidationError):
            p.Grant.from_wire(bad)
    selected = p.Grant.from_wire(grant_data(resource_scope="selected_resources",
                                            resource_ids=["task_1"]))
    assert selected.resource_ids == ["task_1"]


# --------------------------------------------------------------------------- frames


def test_frame_roundtrip_and_hash() -> None:
    grant = p.Grant.from_wire(grant_data())
    semantics = {
        "export_id": grant.target.export_id, "export_revision": 1, "grant_id": grant.grant_id,
        "expected_revision": 0, "revision": 1, "grant": grant.model_dump(mode="json"),
    }
    put = p.GrantPut.from_wire({**semantics, "hash": p.record_hash(semantics)})
    request_id = uid()
    data = p.encode_frame(p.FrameType.GRANT_PUT, request_id, put)
    assert data == p.canonical_json(json.loads(data))
    frame, body = p.parse_frame(data)
    assert frame.type is p.FrameType.GRANT_PUT and frame.request_id == request_id
    assert body == put and p.record_hash(body) == put.hash

    ack = p.MirrorAck(export_id=put.export_id, grant_id=put.grant_id, revision=1,
                      hash=put.hash, state=p.MirrorState.EFFECTIVE)
    assert p.parse_frame(p.encode_frame(p.FrameType.ACK, uid(), ack))[1] == ack


def test_parse_frame_rejects_without_echo() -> None:
    canary = "sk-canary-0123456789"
    bad_frames = [
        {"acp": 1, "type": "shell.exec", "request_id": uid(), "body": {"cmd": canary}},
        {"acp": True, "type": "mirror.get", "request_id": uid(), "body": {"export_id": uid()}},
        {"acp": 2, "type": "mirror.get", "request_id": uid(), "body": {"export_id": uid()}},
        {"acp": 1, "type": "mirror.get", "request_id": "x", "body": {"export_id": uid()}},
        {"acp": 1, "type": "mirror.get", "request_id": uid(), "body": {"export_id": canary}},
        {"acp": 1, "type": "mirror.get", "request_id": uid(), "body": {}, "token": canary},
        [canary],
    ]
    for value in bad_frames:
        with pytest.raises(ProtocolError) as caught:
            p.parse_frame(json.dumps(value))
        assert caught.value.code == "protocol_error"
        assert canary not in str(caught.value) and canary not in repr(caught.value)


def test_grant_put_envelope_must_match_grant() -> None:
    grant = p.Grant.from_wire(grant_data())
    semantics = {
        "export_id": uid(), "export_revision": 1, "grant_id": grant.grant_id,
        "expected_revision": 0, "revision": 1, "grant": grant.model_dump(mode="json"),
    }
    with pytest.raises(ValidationError):
        p.GrantPut.from_wire({**semantics, "hash": p.record_hash(semantics)})
    with pytest.raises(ValidationError):  # revision must be expected + 1
        p.ExportUnpublish.from_wire({"export_id": uid(), "expected_revision": 1,
                                     "revision": 3, "hash": "c" * 64})


# --------------------------------------------------------------------------- discovery


def caps(**overrides) -> bytes:
    data = {"acp": 1, "server_instance_id": SERVER, "ai_control": {"versions": [1]}}
    data.update(overrides)
    return json.dumps(data).encode()


SERVER = str(uuid.uuid4())


def test_capabilities_supported() -> None:
    outcome = p.classify_capabilities(200, caps())
    assert outcome == p.DiscoveryOutcome(p.DiscoveryKind.SUPPORTED, None, SERVER)


@pytest.mark.parametrize(("status", "body", "redirected", "kind", "code"), [
    (404, b"", False, p.DiscoveryKind.UNSUPPORTED, "not_mounted"),
    (401, b"", False, p.DiscoveryKind.AUTH_FAILURE, "auth_failure"),
    (301, b"", False, p.DiscoveryKind.ERROR, "redirect_refused"),
    (307, b"", False, p.DiscoveryKind.ERROR, "redirect_refused"),
    (200, caps(), True, p.DiscoveryKind.ERROR, "redirect_refused"),
    (500, b"", False, p.DiscoveryKind.ERROR, "remote_error"),
    (403, b"", False, p.DiscoveryKind.ERROR, "remote_error"),
    (200, caps(acp=2, ai_control={"versions": [2]}), False,
     p.DiscoveryKind.UNSUPPORTED, "incompatible_version"),
    (200, b"{", False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, b"", False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, b"[]", False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, caps(extra=1), False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, caps(ai_control={"versions": [1], "extra": 1}), False,
     p.DiscoveryKind.ERROR, "invalid_response"),
    (200, caps()[:-1] + b',"acp":1}', False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, caps(acp=True), False, p.DiscoveryKind.ERROR, "invalid_response"),
    (200, caps(ai_control={"versions": [True]}), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(ai_control={"versions": []}), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(ai_control={"versions": [1, 1]}), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(ai_control={"versions": [1, 2]}), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(server_instance_id="not-a-uuid"), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(server_instance_id=SERVER.upper()), False, p.DiscoveryKind.ERROR,
     "invalid_response"),
    (200, caps(acp=1.0), False, p.DiscoveryKind.ERROR, "invalid_response"),
])
def test_capabilities_outcomes(status: int, body: bytes, redirected: bool,
                               kind: p.DiscoveryKind, code: str) -> None:
    outcome = p.classify_capabilities(status, body, redirected=redirected)
    assert (outcome.kind, outcome.code, outcome.server_instance_id) == (kind, code, None)


@pytest.mark.parametrize("body", [
    pytest.param(caps(acp=2, server_instance_id="not-a-uuid", ai_control={"versions": [2]}), id="uuid"),
    pytest.param(caps(acp=2, ai_control={"versions": [2]}, extra=True), id="extra-field"),
    pytest.param(caps(acp=2, ai_control={"versions": [2], "extra": True}), id="nested-extra-field"),
    pytest.param(caps(acp=2, ai_control={"versions": [2, 2]}), id="duplicate-versions"),
    pytest.param(caps(acp=2, ai_control={"versions": list(range(2, 19))}), id="too-many-versions"),
    pytest.param(caps(acp=2, ai_control={"versions": [2, True]}), id="boolean-version"),
])
def test_malformed_future_capabilities_are_not_mislabeled_unsupported(body: bytes) -> None:
    assert p.classify_capabilities(200, body) == p.DiscoveryOutcome(p.DiscoveryKind.ERROR, "invalid_response")


def test_transport_failures_are_unavailable_with_safe_codes() -> None:
    assert p.classify_transport_failure("timeout").kind is p.DiscoveryKind.UNAVAILABLE
    assert p.classify_transport_failure("tls").code == "tls"
    assert p.classify_transport_failure("C:\\secret\\path").code == "network"
    assert {"x-vxs-client-id", "x-vxs-device-token"} == p.FORBIDDEN_DISCOVERY_HEADERS
    assert p.DISCOVERY_REQUEST_HEADERS["Cache-Control"] == "no-store"


# --------------------------------------------------------------------------- admission clock


def test_check_issued_at_bounds() -> None:
    now = 10_000_000
    p.check_issued_at(now - 60_000, now)
    p.check_issued_at(now + 30_000, now)
    for issued in (now - 60_001, now + 30_001):
        with pytest.raises(ProtocolError) as caught:
            p.check_issued_at(issued, now)
        assert caught.value.code == "expired"
    with pytest.raises(ProtocolError):
        p.check_issued_at(True, now)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- isolation


def test_documents_protocol_version_unchanged() -> None:
    from voxarium_studio.sync import protocol as documents

    assert documents.PROTOCOL_VERSION == 1


def test_application_import_does_not_load_ai_access_runtime(tmp_path: Path) -> None:
    """Route and transport types may load; the executor and service are lifespan-owned."""
    env = {**os.environ, "VOXARIUM_STUDIO_HOME": str(tmp_path / "home")}
    code = (
        "import sys, voxarium_studio.main\n"
        "runtime = {'service', 'executor'}\n"
        "prefix = 'voxarium_studio.ai_access.'\n"
        "loaded = runtime & {m[len(prefix):] for m in sys.modules if m.startswith(prefix)}\n"
        "assert not loaded, loaded\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True,
        text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]

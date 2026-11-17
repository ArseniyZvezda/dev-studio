"""VS-180: shared v2 declarations do not release the legacy Studio runner (§16.7)."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from voxarium_studio.sync import protocol as studio
from voxarium_studio.sync.service import RemoteSync, _Stop
from voxarium_studio_remote import protocol as remote
from voxarium_studio_remote.app import create_app
from voxarium_studio_remote.auth import hash_token, new_token
from voxarium_studio_remote.store import Store


@pytest.mark.parametrize("value", ["a", "abc-123._", "a" * 64,
                                   "1234567890abcdef1234567890abcdef",
                                   "01234567-89ab-4cde-8fab-0123456789ab"])
def test_exact_v2_ids_match(value):
    assert studio.check_project_id(value) == remote.check_project_id(value) == value


@pytest.mark.parametrize("value", [None, True, 1, "", "A", " a", "a ", "я", "/a",
                                   "a/b", "a\\b", "a%2fb", "a\n", "a" * 65])
def test_invalid_v2_ids_fail_on_both_sides(value):
    for module in (studio, remote):
        with pytest.raises(ValueError):
            module.check_project_id(value)


@pytest.mark.parametrize("value,expected", [("  Анна  ", "Анна"), ("e\u0301", "é"),
                                            ("same label", "same label")])
def test_display_name_contract(value, expected):
    assert studio.check_display_name(value) == remote.check_display_name(value) == expected


@pytest.mark.parametrize("value", [None, True, "", " ", "a\n", "a\t", "a\x7f",
                                   "a\x85", "a\u2028", "a\u2029", "a\u202e", "a\u2066", "a" * 121])
def test_invalid_display_names(value):
    for module in (studio, remote):
        with pytest.raises(ValueError):
            module.check_display_name(value)


def test_staged_version_does_not_release_runner():
    assert remote.PROTOCOL_VERSION == studio.WIRE_V2_VERSION == 2
    assert studio.CATALOG_API_VERSION == remote.CATALOG_API_VERSION == 2
    assert studio.V2_CAPABILITIES == remote.CAPABILITIES
    assert studio.PROTOCOL_VERSION == studio.LEGACY_RUNNER_PROTOCOL_VERSION == 1
    assert studio.socket_url("https://example.test/prefix", "project") == (
        "wss://example.test/prefix/api/v1/sync?space=project")
    assert studio.CLOSE_PROJECT_NOT_FOUND == remote.CLOSE_NOT_FOUND == 4404


@pytest.mark.asyncio
async def test_actual_legacy_runner_rejects_v2_before_state_or_send():
    class Socket:
        async def recv(self):
            return json.dumps({"t": "hello", "protocol": 2, "seq": 0,
                               "space": {"id": "project", "storage_epoch": "e"}})

    # Real owner method: no paths, state or send facility exists on this object.
    # Accessing any of them before rejecting v2 fails rather than concealing a write.
    runner = SimpleNamespace(_binding_quiescing=False, _binding_fenced=False)
    with pytest.raises(_Stop, match="protocol 2"):
        await RemoteSync._converse(runner, Socket(), "https://example.test")


@pytest.mark.asyncio
async def test_real_remote_hello_is_not_permission_to_activate_legacy_runner():
    store = Store(":memory:")
    token = new_token()
    store.add_user("Contract peer", hash_token(token))
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(create_app(store=store)) as client:
            created = client.post("/api/v2/projects", headers=headers,
                                  json={"id": "project", "display_name": "Project"})
            assert created.status_code == 201
            assert client.get("/api/v2/me", headers=headers).json()["protocol"] == 2
            for selector in ("project", "typo"):
                with pytest.raises(WebSocketDisconnect) as closed:
                    with client.websocket_connect(f"/api/v1/sync?space={selector}", headers=headers) as ws:
                        ws.receive_json()
                assert closed.value.code == 4400
            with client.websocket_connect("/api/v2/sync?space=project", headers=headers) as ws:
                hello = ws.receive_json()
                assert hello["t"] == "hello" and hello["protocol"] == 2

                class Socket:
                    async def recv(self):
                        return json.dumps(hello)

                runner = SimpleNamespace(_binding_quiescing=False, _binding_fenced=False)
                with pytest.raises(_Stop, match="protocol 2"):
                    await RemoteSync._converse(runner, Socket(), "https://example.test")
            assert len(store.list_spaces()) == 1
            assert store.project("project").seq == 0
    finally:
        store.close()

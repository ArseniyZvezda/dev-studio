"""HTTP-by-IP contract (dev spec §16.2), including the real Remote HTTP routes."""
from contextlib import asynccontextmanager
import ssl
from uuid import uuid4

import httpx
import pytest
from voxarium_studio_remote.app import create_app
from voxarium_studio_remote.auth import hash_token
from voxarium_studio_remote.store import Store

from voxarium_studio.ai_access.transport import AiTransport, DeviceCredentials
from voxarium_studio.sync.catalog import CatalogClient
from voxarium_studio.sync.catalog_http import CatalogHttpTransport
from voxarium_studio.sync.protocol import normalise_url


@pytest.mark.parametrize("raw, expected", [
    ("192.0.2.10", "http://192.0.2.10"),
    ("192.0.2.10:8080/prefix", "http://192.0.2.10:8080/prefix"),
    ("[2001:db8::1]/prefix", "http://[2001:db8::1]/prefix"),
    ("relay.example/prefix", "https://relay.example/prefix"),
    ("https://192.0.2.10", "https://192.0.2.10"),
])
def test_bare_ip_uses_http_but_explicit_https_is_preserved(raw, expected):
    assert normalise_url(raw) == expected


async def test_http_catalog_authenticates_and_reads_real_remote_without_tls(tmp_path):
    store = Store(tmp_path / "remote.db")
    key = "vxs_synthetic_http_ip_key"
    user = store.create_user("HTTP user", hash_token(key))
    store.create_project("game", "Game")
    try:
        async with CatalogHttpTransport(transport=httpx.ASGITransport(app=create_app(store=store))) as wire:
            client = CatalogClient(wire, url="http://192.0.2.10", token=key)
            verified = await client.verify()
            assert verified.user.id == user.id
            projects = await client.list_projects()
            assert [project.id for project in projects.projects] == ["game"]
    finally:
        store.close()


@pytest.mark.parametrize("secure", [False, True])
async def test_ai_socket_preserves_scheme_credentials_and_tls_validation(secure):
    calls = []
    key = "vxs_synthetic_http_ip_key"
    device = DeviceCredentials(str(uuid4()), "vxd_" + "a" * 43)

    @asynccontextmanager
    async def connect(url, **options):
        calls.append((url, options))
        yield object()

    base = ("https" if secure else "http") + "://192.0.2.10/prefix"
    async with AiTransport(ws_connect=connect).connect(base, key, device):
        pass
    url, options = calls[0]
    assert url == ("wss" if secure else "ws") + "://192.0.2.10/prefix/api/ai/v1/control"
    assert options["additional_headers"]["Authorization"] == "Bearer " + key
    assert options["additional_headers"]["X-VXS-Device-Token"] == device.token
    assert options["proxy"] is None
    if secure:
        assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert options["ssl"].check_hostname
    else:
        assert options["ssl"] is None

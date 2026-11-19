"""C2 bounded HTTP boundary (activation-plan §2/§4), without sockets or Remote.

Low-level transports are trusted test injection, never an ambient AsyncClient.
These checks do not prove real TLS interoperability or POST non-delivery.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
import traceback

import httpx
import httpcore
import pytest

from voxarium_studio.sync.catalog import CatalogError, CatalogResponse
from voxarium_studio.sync import catalog_http
from voxarium_studio.sync.catalog_http import CatalogHttpTransport


BASE = "https://catalog.invalid/deployment/team"
KEY = "C2-SYNTHETIC-KEY-CANARY"
SERVER_SECRET = "C2-SERVER-SECRET-CANARY"
LIMIT = 4 * 1024 * 1024
AUTH = {"Authorization": "Bearer " + KEY}


class Stream(httpx.AsyncByteStream):
    def __init__(self, *chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.reads = 0
        self.closed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error is not None:
            raise self.error

    async def aclose(self):
        self.closed += 1


class Wire(httpx.AsyncBaseTransport):
    def __init__(self, stream=None, *, status=200, headers=None, handler=None):
        self.stream = stream if stream is not None else Stream(b'{"ok":true}')
        self.status = status
        self.headers = {"Content-Type": "application/json"} if headers is None else headers
        self.handler = handler
        self.calls = []
        self.closed = 0

    async def handle_async_request(self, request):
        self.calls.append(request)
        if self.handler is not None:
            return await self.handler(request)
        return httpx.Response(self.status, headers=self.headers, stream=self.stream)

    async def aclose(self):
        self.closed += 1


@pytest.fixture(autouse=True)
async def no_network(monkeypatch):
    # Create the Windows event loop's internal socketpair before denying network
    # I/O. Only the HTTP boundary below is under test, not asyncio's self-pipe.
    def forbidden(*args, **kwargs):
        pytest.fail("C2 mock tests must not open sockets or resolve DNS")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


async def request(client, method="GET", path="/api/v2/projects", **changes):
    kwargs = dict(headers=AUTH.copy(), params={}, json=None, follow_redirects=False)
    kwargs.update(changes)
    return await client.request(method, BASE + path, **kwargs)


def sanitized(error, code):
    assert type(error) is CatalogError
    assert error.code == code
    assert error.args == (code,)
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = repr(error) + "".join(traceback.format_exception(error))
    assert KEY not in rendered
    assert SERVER_SECRET not in rendered
    assert BASE not in rendered


@pytest.mark.parametrize("method,path,params,body", [
    ("GET", "/healthz", {}, None),
    ("GET", "/api/v1/me", {}, None),
    ("GET", "/api/v2/me", {}, None),
    ("GET", "/api/v2/projects", {"limit": 37, "after": "a._-1"}, None),
    ("GET", "/api/v2/projects/a._-1", {}, None),
    ("GET", "/api/v2/projects/healthz", {}, None),
    ("POST", "/api/v2/projects", {}, {"id": "a", "display_name": "Тест"}),
])
async def test_exact_wire_prefix_query_method_auth_and_owned_close(method, path, params, body):
    wire = Wire(headers={"Content-Type": "application/json; charset=utf-8",
                         "Set-Cookie": SERVER_SECRET, "X-Secret": SERVER_SECRET})
    async with CatalogHttpTransport(transport=wire) as client:
        result = await request(client, method, path, headers={} if path == "/healthz" else AUTH,
                               params=params, json=body)
        assert isinstance(result, CatalogResponse)
        assert result.status == 200 and result.body == {"ok": True}
        assert result.headers == {}
        assert wire.closed == 0
        assert wire.stream.closed == 1
        assert len(wire.calls) == 1
        sent = wire.calls[0]
        assert sent.method == method
        assert str(sent.url).split("?", 1)[0] == BASE + path
        assert sent.url.path == "/deployment/team" + path
        assert dict(sent.url.params) == {k: str(v) for k, v in params.items()}
        assert sent.url.query == (b"limit=37&after=a._-1" if params else b"")
        assert sent.headers["accept"] == "application/json"
        assert sent.headers["cache-control"] == "no-store"
        assert sent.headers["accept-encoding"] == "identity"
        assert "cookie" not in sent.headers
        assert "proxy-authorization" not in sent.headers
        assert sent.headers.get("authorization") == (None if path == "/healthz" else "Bearer " + KEY)
        assert KEY not in str(sent.url)
        assert sent.extensions["timeout"] == {"connect": 5, "pool": 5, "read": 15, "write": 15}
        if body is None:
            assert sent.content == b""
        else:
            assert json.loads(sent.content) == body
            assert sent.headers["content-type"] == "application/json"
    assert wire.closed == 1
    await client.aclose()
    assert wire.closed == 1
    assert KEY not in repr(client) and SERVER_SECRET not in repr(result)


@pytest.mark.parametrize("path,params,query", [
    ("/api/v2/projects/a._-1/snapshot", {"limit": 20}, b"limit=20"),
    ("/api/v2/projects/a/snapshot", {"limit": 1, "expected_epoch": "a" * 32},
     b"limit=1&expected_epoch=" + b"a" * 32),
    ("/api/v2/projects/healthz/snapshot", {"limit": 100, "after": "dir/b c.md", "expected_seq": 7,
                                           "expected_epoch": "b" * 32},
     b"limit=100&after=dir%2Fb+c.md&expected_seq=7&expected_epoch=" + b"b" * 32),
    ("/api/v2/projects/snapshot", {}, b""),  # "snapshot" is an ordinary exact project ID.
    ("/api/v2/legacy-spaces/resolve", {"name": "Old Space/ü&x=1"},
     b"name=Old+Space%2F%C3%BC%26x%3D1"),
])
async def test_s3_snapshot_and_resolve_are_exact_get_only_reads(path, params, query):
    """I11: the production pool sends exactly these read-only GET forms."""
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        result = await request(client, "GET", path, params=params)
        assert result.status == 200 and result.body == {"ok": True}
    sent = wire.calls[0]
    assert len(wire.calls) == 1 and sent.method == "GET" and sent.content == b""
    assert sent.url.path == "/deployment/team" + path
    assert sent.url.query == query
    assert sent.headers["authorization"] == "Bearer " + KEY
    assert KEY not in str(sent.url)
    assert wire.stream.closed == 1


async def test_s3_snapshot_response_is_bounded_like_every_response(monkeypatch):
    wire = Wire(Stream(b"{" + b" " * LIMIT, b"}"))
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client, "GET", "/api/v2/projects/a/snapshot", params={"limit": 20})
    sanitized(caught.value, "invalid_response")
    assert len(wire.calls) == 1 and wire.stream.closed == 1


async def test_cookie_response_is_not_forwarded_on_next_request():
    wire = Wire(headers={"Content-Type": "application/json", "Set-Cookie": "key=" + KEY})
    async with CatalogHttpTransport(transport=wire) as client:
        await request(client)
        wire.stream = Stream(b"{}")
        await request(client)
    assert len(wire.calls) == 2
    assert all("cookie" not in call.headers for call in wire.calls)


@pytest.mark.parametrize("prefix", ["", "/deployment/team", "/api/v2/projects",
                                   "/deployment/api/v2/projects"])
async def test_overlapping_prefix_health_and_healthz_project_keep_auth_intent(prefix):
    # The same URL spelling can be a deployment's health endpoint or a project
    # named healthz. No URL repair, auth fallback, or extra discovery send.
    url = "https://catalog.invalid" + prefix + "/healthz"
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        await client.request("GET", url, headers={}, params={}, json=None,
                             follow_redirects=False)
        assert "authorization" not in wire.calls[-1].headers
        wire.stream = Stream(b"{}")
        project = "https://catalog.invalid" + prefix + "/api/v2/projects/healthz"
        await client.request("GET", project, headers=AUTH, params={}, json=None,
                             follow_redirects=False)
        assert wire.calls[-1].headers["authorization"] == "Bearer " + KEY
    assert [str(call.url) for call in wire.calls] == [url, project]


@pytest.mark.parametrize("extra", [{"Cookie": KEY}, {"Proxy-Authorization": KEY},
                                   {"Host": "second.invalid"}, {"X-Key": KEY},
                                   {"authorization": "Bearer " + SERVER_SECRET}])
async def test_ambient_or_duplicate_authorization_headers_never_reach_wire(extra):
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client, headers={**AUTH, **extra})
    sanitized(caught.value, "invalid_request")
    assert wire.calls == []


async def test_production_uses_verified_environment_independent_low_level_transport(monkeypatch):
    created = []
    wire = Wire()

    def build(**kwargs):
        created.append(kwargs)
        return wire

    def no_client(*args, **kwargs):
        pytest.fail("Catalog transport must not construct ambient httpx.AsyncClient")

    monkeypatch.setenv("HTTPS_PROXY", "http://" + KEY + ".invalid")
    monkeypatch.setenv("SSL_CERT_FILE", SERVER_SECRET)
    monkeypatch.setenv("NETRC", SERVER_SECRET)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", build)
    monkeypatch.setattr(httpx, "AsyncClient", no_client)
    async with CatalogHttpTransport() as client:
        await request(client)
    assert len(created) == 1
    assert created[0]["verify"] is True
    assert created[0]["trust_env"] is False
    assert created[0]["retries"] == 0
    assert created[0].get("proxy") is None
    assert wire.closed == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [BASE + "/api/v2/projects", "https://second.invalid/" + KEY])
async def test_redirect_never_reads_body_or_sends_again(status, location):
    wire = Wire(Stream(b"not json " + KEY.encode()), status=status,
                headers={"Location": location, "Content-Type": "text/plain"})
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client, "POST", json={"id": "a", "display_name": "A"})
    sanitized(caught.value, "redirect_refused")
    assert len(wire.calls) == 1
    assert wire.stream.reads == 0 and wire.stream.closed == 1


@pytest.mark.parametrize("method,path,changes", [
    ("DELETE", "/api/v2/projects", {}),
    ("get", "/api/v2/projects", {}),
    ("POST", "/api/v2/me", {}),
    ("GET", "/api/v2/projects/a/snapshot", {}),
    ("GET", "/api/v2/projects/a/", {}),
    # I11/S3: snapshot and legacy resolve are GET-only with exact query sets.
    ("POST", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}}),
    ("POST", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}, "json": {"id": "a"}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}, "json": {"id": "a"}}),
    ("PUT", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}}),
    ("DELETE", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20, "space": "a"}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20, "after": "a.md"}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20, "after": "a.md", "expected_seq": 1}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"after": "a.md", "expected_seq": 1,
                                                       "expected_epoch": "a" * 32}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}, "headers": {}}),
    ("GET", "/api/v2/projects/a/snapshot/", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/a/snapshots", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/A/snapshot", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/a/b/snapshot", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/a/sync", {"params": {"limit": 20}}),
    ("GET", "/api/v2/projects/a/documents", {}),
    ("GET", "/api/v2/sync", {"params": {"space": "a"}}),
    ("GET", "/api/v2/legacy-spaces/resolve", {}),
    ("GET", "/api/v2/legacy-spaces/resolve", {"params": {"name": "x", "limit": 1}}),
    ("GET", "/api/v2/legacy-spaces/resolve", {"params": {"name": 7}}),
    ("GET", "/api/v2/legacy-spaces/resolve", {"params": {"name": "x"}, "headers": {}}),
    ("POST", "/api/v2/legacy-spaces/resolve", {"params": {"name": "x"}}),
    ("POST", "/api/v2/legacy-spaces/resolve", {"json": {"name": "x"}}),
    ("GET", "/api/v2/legacy-spaces", {"params": {"name": "x"}}),
    ("GET", "/api/v2/legacy-spaces/resolve/", {"params": {"name": "x"}}),
    ("GET", "/api/v2/projects/a/snapshot", {"params": {"limit": 20}, "follow_redirects": True}),
    ("GET", "/api/v2/projects", {"follow_redirects": True}),
    ("GET", "/healthz", {"headers": AUTH}),
    ("GET", "/api/v2/me", {"headers": {}}),
    ("GET", "/api/v2/me", {"headers": {"Authorization": "Basic " + KEY}}),
    ("GET", "/api/v2/me", {"headers": {"Authorization": "Bearer " + KEY + "\r\nX: bad"}}),
])
async def test_invalid_request_fails_before_send(method, path, changes):
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client, method, path, **changes)
    sanitized(caught.value, "invalid_request")
    assert wire.calls == []


@pytest.mark.parametrize("url", [
    "https://user:" + KEY + "@catalog.invalid/api/v2/me",
    BASE + "/api/v2/me?key=" + KEY,
    BASE + "/api/v2/me#" + KEY,
    "https://CATALOG.invalid/deployment/api/v2/me",
    "https://catalog.invalid:443/deployment/api/v2/me",
    "https://catalog.invalid/prefix/../api/v2/me",
    "https://catalog.invalid/prefix/%2e%2e/api/v2/me",
    "https://catalog.invalid/путь/api/v2/me",
    "https://catalog.invalid/prefix\\other/api/v2/me",
])
async def test_url_must_be_strict_exact_and_not_retargeted_by_httpx(url):
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await client.request("GET", url, headers=AUTH, params={}, json=None, follow_redirects=False)
    sanitized(caught.value, "invalid_request")
    assert wire.calls == []


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_valid_double_slash_prefix_is_preserved_not_normalized(scheme):
    # _url deliberately does not collapse prefix slashes: matching is exact,
    # not an invented canonicalization policy at this lower boundary.
    url = f"{scheme}://catalog.invalid/prefix//api/v2/me"
    wire = Wire()
    async with CatalogHttpTransport(transport=wire) as client:
        await client.request("GET", url, headers=AUTH, params={}, json=None, follow_redirects=False)
    assert str(wire.calls[0].url) == url


@pytest.mark.parametrize("body", [
    b"[]", b"null", b"true", b"1", b'"string"', b"", b"{", b"{} trailing",
    b'{"a":1,"a":2}', b'{"a":{"x":1,"x":2}}', b'{"a":NaN}',
    b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":1e400}',
    b'{"a":[{"b":1e999}]}', b'{"a":"\xff"}', b'\xff\xfe{\x00}\x00', b'\xef\xbb\xbf{}',
    b'{"a":"\\ud800"}', b'{"\\udfff":1}',
])
async def test_strict_utf8_json_object_parser(body):
    wire = Wire(Stream(body))
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.closed == 1


@pytest.mark.parametrize("content_type", [None, "text/json", "text/plain", "application/problem+json",
                                          "application/json; charset=iso-8859-1"])
async def test_json_content_type_is_mandatory(content_type):
    wire = Wire(headers={} if content_type is None else {"Content-Type": content_type})
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.closed == 1


@pytest.mark.parametrize("headers", [
    [("Content-Type", "application/json"), ("Content-Type", "application/json")],
    [("Content-Type", "application/json"), ("Content-Length", "2"), ("Content-Length", "2")],
    {"Content-Type": "application/json", "Content-Length": "-1"},
    {"Content-Type": "application/json", "Content-Length": "not-a-number"},
])
async def test_ambiguous_response_framing_headers_fail_closed(headers):
    wire = Wire(Stream(b"{}"), headers=headers)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.reads == 0 and wire.stream.closed == 1


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
async def test_compressed_response_rejected_without_reading_or_decompression(encoding):
    wire = Wire(Stream(b"{}"), headers={"Content-Type": "application/json", "Content-Encoding": encoding})
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.reads == 0 and wire.stream.closed == 1


async def test_declared_oversize_rejected_before_read_or_json_decode(monkeypatch):
    def no_decode(*args, **kwargs):
        pytest.fail("raw byte bound must run before JSON decode")

    monkeypatch.setattr(json, "loads", no_decode)
    wire = Wire(Stream(b"{}"), headers={"Content-Type": "application/json", "Content-Length": str(LIMIT + 1)})
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.reads == 0 and wire.stream.closed == 1


@pytest.mark.parametrize("declared", [None, "2"])
async def test_stream_limit_independent_of_absent_or_false_content_length(declared, monkeypatch):
    def no_decode(*args, **kwargs):
        pytest.fail("stream byte bound must run before JSON decode")

    monkeypatch.setattr(json, "loads", no_decode)
    headers = {"Content-Type": "application/json"}
    if declared is not None:
        headers["Content-Length"] = declared
    # A third chunk must never be consumed once the raw byte bound is exceeded.
    wire = Wire(Stream(b" " * LIMIT, b" " * 1, b"{}"), headers=headers)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "invalid_response")
    assert wire.stream.reads == 2 and wire.stream.closed == 1


async def test_exact_four_mib_object_is_accepted_with_identity_encoding():
    wire = Wire(Stream(b'{"value":"' + b"a" * (LIMIT - 12) + b'"}'),
                headers={"Content-Type": "application/json", "Content-Encoding": "identity"})
    async with CatalogHttpTransport(transport=wire) as client:
        result = await request(client)
    assert len(result.body["value"]) == LIMIT - 12
    assert wire.stream.closed == 1


@pytest.mark.parametrize("status", [400, 401, 404, 409, 413, 500, 503])
async def test_nonredirect_error_status_preserves_only_decoded_object(status):
    body = {"error": {"code": "unauthorised", "message": SERVER_SECRET, "request_id": KEY}}
    wire = Wire(Stream(json.dumps(body).encode()), status=status,
                headers={"Content-Type": "application/json", "X-Secret": SERVER_SECRET})
    async with CatalogHttpTransport(transport=wire) as client:
        result = await request(client)
    assert (result.status, result.body, result.headers) == (status, body, {})
    assert KEY not in repr(result) and SERVER_SECRET not in repr(result)
    assert wire.stream.closed == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ReadTimeout,
                                       httpx.WriteTimeout, httpx.ConnectError, httpx.ReadError,
                                       httpx.RemoteProtocolError, RuntimeError])
@pytest.mark.parametrize("during_body", [False, True])
async def test_phase_errors_sanitized_and_no_retry_even_for_post(error_type, during_body):
    error = error_type(KEY + " " + SERVER_SECRET + " " + BASE)

    async def fail(_):
        raise error

    stream = Stream(b"{", error=error)
    wire = Wire(stream, handler=None if during_body else fail)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client, "POST", json={"id": "a", "display_name": "A"})
    sanitized(caught.value, "invalid_response" if during_body and error_type is RuntimeError
              else "transport_unavailable")
    assert len(wire.calls) == 1
    assert stream.closed == int(during_body)


@pytest.mark.parametrize("during_body", [False, True])
async def test_total_deadline_covers_send_and_body_with_single_post(monkeypatch, during_body):
    monkeypatch.setattr(catalog_http, "_TOTAL_TIMEOUT", 0.03)
    monkeypatch.setattr(catalog_http, "_CLEANUP_TIMEOUT", 0.005)

    class Slow(Stream):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b"{}"

    async def wait(_):
        await asyncio.Event().wait()

    wire = Wire(Slow(), handler=None if during_body else wait)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(request(client, "POST", json={"id": "a", "display_name": "A"}), 0.5)
    sanitized(caught.value, "transport_unavailable")
    assert len(wire.calls) == 1
    assert wire.stream.closed == int(during_body)


@pytest.mark.parametrize("during_body", [False, True])
async def test_transport_cancel_has_fresh_empty_exception_and_closes_response(during_body):
    original = asyncio.CancelledError(KEY + " " + SERVER_SECRET)

    async def cancel(_):
        raise original

    wire = Wire(Stream(error=original), handler=None if during_body else cancel)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(asyncio.CancelledError) as caught:
            await request(client, "POST", json={"id": "a", "display_name": "A"})
    assert caught.value is not original
    assert caught.value.args == ()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(wire.calls) == 1
    assert wire.stream.closed == int(during_body)


async def test_caller_cancel_closes_response_before_propagating_sanitized_cancel():
    reading = asyncio.Event()
    closing = asyncio.Event()
    permit_close = asyncio.Event()

    class Blocking(Stream):
        async def __aiter__(self):
            reading.set()
            await asyncio.Event().wait()
            yield b"{}"

        async def aclose(self):
            closing.set()
            await permit_close.wait()
            await super().aclose()

    wire = Wire(Blocking())
    async with CatalogHttpTransport(transport=wire) as client:
        task = asyncio.create_task(request(client, "POST", json={"id": "a", "display_name": "A"}))
        await asyncio.wait_for(reading.wait(), 1)
        task.cancel(KEY)
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel(SERVER_SECRET)  # cleanup must survive a second cancellation.
        permit_close.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert wire.stream.closed == 1
    assert caught.value.args == ()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(wire.calls) == 1


async def test_cancellation_during_success_eof_close_still_finishes_cleanup():
    closing = asyncio.Event()
    permit_close = asyncio.Event()

    class SlowClose(Stream):
        async def aclose(self):
            closing.set()
            await permit_close.wait()
            await super().aclose()

    wire = Wire(SlowClose(b"{}"))
    async with CatalogHttpTransport(transport=wire) as client:
        task = asyncio.create_task(request(client))
        await asyncio.wait_for(closing.wait(), 1)
        task.cancel(KEY)
        permit_close.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert wire.stream.closed == 1
    assert caught.value.args == ()
    assert caught.value.__cause__ is None and caught.value.__context__ is None


async def test_request_after_owned_close_does_not_send():
    wire = Wire()
    client = CatalogHttpTransport(transport=wire)
    await client.aclose()
    with pytest.raises(CatalogError) as caught:
        await request(client)
    sanitized(caught.value, "transport_unavailable")
    assert wire.calls == [] and wire.closed == 1


async def test_pool_constructor_error_has_no_secret_exception_chain(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError(KEY + " " + SERVER_SECRET + " " + BASE)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", fail)
    with pytest.raises(CatalogError) as caught:
        CatalogHttpTransport()
    sanitized(caught.value, "transport_unavailable")


async def test_owned_pool_close_error_is_sanitized_and_close_is_idempotent():
    class BrokenPool(Wire):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError(KEY + " " + SERVER_SECRET)

    wire = BrokenPool()
    client = CatalogHttpTransport(transport=wire)
    with pytest.raises(CatalogError) as caught:
        await client.aclose()
    sanitized(caught.value, "transport_unavailable")
    await client.aclose()
    assert wire.closed == 1


async def test_close_failure_is_sanitized_and_never_retried():
    class BadClose(Stream):
        async def aclose(self):
            await super().aclose()
            raise RuntimeError(KEY + " " + SERVER_SECRET)

    wire = Wire(BadClose(b"{}"))
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "transport_unavailable")
    assert wire.stream.closed == 1 and len(wire.calls) == 1


def test_declared_production_deadlines_include_reserved_cleanup():
    assert catalog_http._TOTAL_TIMEOUT == 30
    assert catalog_http._CLEANUP_TIMEOUT == 5
    assert catalog_http._PHASE_TIMEOUTS == {"connect": 5, "pool": 5, "read": 15, "write": 15}


@pytest.mark.parametrize("outcome", ["success", "redirect", "parser", "total_timeout", "caller_cancel",
                                    "cancel_during_cleanup"])
async def test_hanging_response_close_is_bounded_and_poisons_pool(monkeypatch, outcome):
    monkeypatch.setattr(catalog_http, "_TOTAL_TIMEOUT", 0.03)
    monkeypatch.setattr(catalog_http, "_CLEANUP_TIMEOUT", 0.005)
    reading = asyncio.Event()
    closing = asyncio.Event()
    close_terminated = asyncio.Event()

    class HangingClose(Stream):
        async def __aiter__(self):
            reading.set()
            if outcome in {"total_timeout", "caller_cancel"}:
                await asyncio.Event().wait()
            yield b"not json" if outcome == "parser" else b"{}"

        async def aclose(self):
            closing.set()
            try:
                await asyncio.Event().wait()
            finally:
                close_terminated.set()

    wire = Wire(HangingClose(), status=302 if outcome == "redirect" else 200)
    async with CatalogHttpTransport(transport=wire) as client:
        task = asyncio.create_task(request(client, "POST", json={"id": "a", "display_name": "A"}))
        if outcome == "caller_cancel":
            await asyncio.wait_for(reading.wait(), 0.5)
            task.cancel(KEY)
        await asyncio.wait_for(closing.wait(), 0.5)
        if outcome == "cancel_during_cleanup":
            task.cancel(KEY)
            await asyncio.sleep(0)
            task.cancel(SERVER_SECRET)
        if outcome in {"caller_cancel", "cancel_during_cleanup"}:
            with pytest.raises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 0.5)
            assert caught.value.args == ()
            assert caught.value.__cause__ is None and caught.value.__context__ is None
        else:
            with pytest.raises(CatalogError) as caught:
                await asyncio.wait_for(task, 0.5)
            sanitized(caught.value, "transport_unavailable")
        # Cleanup's own deadline, not the outer test watchdog, stopped the
        # cooperative resource. No abandoned close task may remain in the pool.
        assert close_terminated.is_set()
        with pytest.raises(CatalogError) as unavailable:
            await request(client)
        sanitized(unavailable.value, "transport_unavailable")
        assert len(wire.calls) == 1
    assert wire.closed == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_owned_pool_hanging_close_has_its_own_bounded_deadline(monkeypatch, cancel):
    monkeypatch.setattr(catalog_http, "_CLEANUP_TIMEOUT", 0.005)
    closing = asyncio.Event()
    terminated = asyncio.Event()

    class HangingPool(Wire):
        async def aclose(self):
            self.closed += 1
            closing.set()
            try:
                await asyncio.Event().wait()
            finally:
                terminated.set()

    wire = HangingPool()
    client = CatalogHttpTransport(transport=wire)
    task = asyncio.create_task(client.aclose())
    await asyncio.wait_for(closing.wait(), 0.5)
    if cancel:
        task.cancel(KEY)
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 0.5)
        assert caught.value.args == ()
        assert caught.value.__cause__ is None and caught.value.__context__ is None
    else:
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(task, 0.5)
        sanitized(caught.value, "transport_unavailable")
    assert terminated.is_set()
    await client.aclose()
    with pytest.raises(CatalogError) as unavailable:
        await request(client)
    sanitized(unavailable.value, "transport_unavailable")
    assert wire.calls == [] and wire.closed == 1


class MemoryNetwork(httpcore.AsyncNetworkBackend):
    """Real httpcore HTTP/1 pool over memory, never a socket or TLS handshake.

    The backend honors received phase deadlines. TLS metadata is inspected, but
    returning the memory stream is explicitly not evidence of TLS interop.
    """

    def __init__(self, *, stalled_phase=None, read_gate=None):
        self.stalled_phase = stalled_phase
        self.read_gate = read_gate
        self.read_started = asyncio.Event()
        self.events = []
        self.streams = []
        self.connects = 0

    async def phase(self, name, timeout):
        self.events.append((name, timeout))
        if self.stalled_phase == name:
            errors = {"connect": httpcore.ConnectTimeout, "tls": httpcore.ConnectTimeout,
                      "read": httpcore.ReadTimeout, "write": httpcore.WriteTimeout}
            try:
                async with asyncio.timeout(timeout):
                    await asyncio.Event().wait()
            except TimeoutError:
                raise errors[name](KEY + " " + SERVER_SECRET) from None

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        assert (host, port) == ("catalog.invalid", 443)
        self.connects += 1
        await self.phase("connect", timeout)
        stream = MemoryNetworkStream(self)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        pytest.fail("unexpected alternate endpoint")

    async def sleep(self, seconds):
        pytest.fail("HTTP transport must not schedule retries")


class MemoryNetworkStream(httpcore.AsyncNetworkStream):
    def __init__(self, backend):
        self.backend = backend
        self.writes = []
        self.closed = 0
        self.reply = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
                      b"Set-Cookie: key=" + SERVER_SECRET.encode() + b"\r\n\r\n{}")

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert ssl_context.check_hostname is True
        assert server_hostname == "catalog.invalid"
        await self.backend.phase("tls", timeout)
        return self

    async def read(self, max_bytes, timeout=None):
        self.backend.read_started.set()
        await self.backend.phase("read", timeout)
        if self.backend.read_gate is not None:
            await self.backend.read_gate.wait()
        result, self.reply = self.reply[:max_bytes], self.reply[max_bytes:]
        return result

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)
        await self.backend.phase("write", timeout)

    async def aclose(self):
        self.closed += 1


class PooledWire(httpx.AsyncHTTPTransport):
    """Retain the real httpx mapping/httpcore pool; replace only network I/O."""

    def __init__(self, backend):
        super().__init__(verify=True, trust_env=False, retries=0, http1=True, http2=False,
                         limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        self._pool._network_backend = backend
        self.calls = []
        self.errors = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        try:
            return await super().handle_async_request(request)
        except Exception as error:
            self.errors.append(type(error))
            raise


async def test_real_httpcore_pool_saturation_times_out_queued_post_without_second_send(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(catalog_http, "_PHASE_TIMEOUTS", {"connect": 5, "pool": 0.01,
                                                       "read": 15, "write": 15})
    gate = asyncio.Event()
    backend = MemoryNetwork(read_gate=gate)
    wire = PooledWire(backend)
    async with CatalogHttpTransport(transport=wire) as client:
        first = asyncio.create_task(request(client, "POST", json={"id": "a", "display_name": "A"}))
        try:
            await asyncio.wait_for(backend.read_started.wait(), 0.5)
            with pytest.raises(CatalogError) as caught:
                await asyncio.wait_for(request(client, "POST", json={"id": "b", "display_name": "B"}), 0.5)
            sanitized(caught.value, "transport_unavailable")
            assert wire.errors == [httpx.PoolTimeout]  # raised by real queue, not the memory backend.
            assert len(wire.calls) == 2
            assert backend.connects == 1
            assert sum(chunk.startswith(b"POST ") for stream in backend.streams
                       for chunk in stream.writes) == 1
            gate.set()
            assert (await asyncio.wait_for(first, 0.5)).body == {}
        finally:
            gate.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)
    assert wire._pool.connections == []
    assert all(stream.closed == 1 for stream in backend.streams)
    assert KEY not in caplog.text and SERVER_SECRET not in caplog.text


@pytest.mark.parametrize("phase,expected", [("connect", httpx.ConnectTimeout), ("tls", httpx.ConnectTimeout),
                                           ("write", httpx.WriteTimeout), ("read", httpx.ReadTimeout)])
async def test_real_httpcore_wires_phase_deadlines_and_sanitizes_backend_timeouts(monkeypatch, caplog,
                                                                               phase, expected):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(catalog_http, "_PHASE_TIMEOUTS", {"connect": 0.01, "pool": 0.01,
                                                       "read": 0.01, "write": 0.01})
    backend = MemoryNetwork(stalled_phase=phase)
    wire = PooledWire(backend)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await asyncio.wait_for(request(client, "POST", json={"id": "a", "display_name": "A"}), 0.5)
    sanitized(caught.value, "transport_unavailable")
    assert wire.errors == [expected]
    assert (phase, 0.01) in backend.events
    assert len(wire.calls) == 1 and backend.connects == 1
    assert sum(chunk.startswith(b"POST ") for stream in backend.streams for chunk in stream.writes) <= 1
    assert wire._pool.connections == []
    assert KEY not in caplog.text and SERVER_SECRET not in caplog.text


async def test_debug_logs_do_not_leak_request_headers_url_body_or_exception(caplog):
    caplog.set_level(logging.DEBUG)

    async def noisy(sent):
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11"):
            logger = logging.getLogger(name)
            logger.debug("request %s headers %s body %s", sent.url, sent.headers, SERVER_SECRET)
            try:
                raise RuntimeError(KEY + " " + SERVER_SECRET)
            except RuntimeError:
                logger.exception("failed %s", KEY)
        raise httpx.ReadError(SERVER_SECRET)

    wire = Wire(handler=noisy)
    async with CatalogHttpTransport(transport=wire) as client:
        with pytest.raises(CatalogError) as caught:
            await request(client)
    sanitized(caught.value, "transport_unavailable")
    assert KEY not in caplog.text and SERVER_SECRET not in caplog.text
    assert BASE not in caplog.text


async def test_http_log_suppression_does_not_disable_other_concurrent_tasks(caplog):
    caplog.set_level(logging.DEBUG)
    inside = asyncio.Event()
    release = asyncio.Event()

    async def wait(_):
        logging.getLogger("httpcore.connection").debug(KEY)
        inside.set()
        await release.wait()
        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Stream(b"{}"))

    wire = Wire(handler=wait)
    async with CatalogHttpTransport(transport=wire) as client:
        task = asyncio.create_task(request(client))
        await asyncio.wait_for(inside.wait(), 1)
        logging.getLogger("httpcore.connection").debug("C2-unrelated-task-visible")
        release.set()
        await task
        logging.getLogger("httpcore.connection").debug("C2-after-request-visible")
    assert KEY not in caplog.text
    assert "C2-unrelated-task-visible" in caplog.text
    assert "C2-after-request-visible" in caplog.text

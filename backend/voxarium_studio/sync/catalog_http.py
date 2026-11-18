"""Owned, bounded HTTP boundary (catalog-api-activation-plan §4 C2).

Not wired to a runtime, API or credential store. The caller supplies the already
guarded exact endpoint on every call. In particular, failure/cancellation of a
POST says nothing about delivery: this boundary never retries or probes a URL.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar
import json as json_module
import logging
import math
import re
from urllib.parse import urlsplit

import httpx

from .catalog import CatalogError, CatalogResponse, _url


_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_TOTAL_TIMEOUT = 30.0
_CLEANUP_TIMEOUT = 5.0
_PHASE_TIMEOUTS = {"connect": 5.0, "pool": 5.0, "read": 15.0, "write": 15.0}
_PRIVATE_IO: ContextVar[bool] = ContextVar("catalog_private_http_io", default=False)


class _PrivateIOFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_IO.get()


_LOG_FILTER = _PrivateIOFilter()


def _protect_logs() -> None:
    # httpcore DEBUG includes response headers and exception reprs. Do not change
    # global log levels/handlers: suppress only this task's I/O, including cleanup
    # tasks inheriting its context. These are httpcore's HTTP/1, HTTP/2 and proxy
    # logger namespaces; proxies/HTTP2 are not enabled by this boundary.
    for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
                 "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
        logger = logging.getLogger(name)
        if _LOG_FILTER not in logger.filters:
            logger.addFilter(_LOG_FILTER)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(_: str) -> object:
    raise ValueError()


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError()
    return number


def _decode(raw: bytearray) -> dict:
    value = json_module.loads(
        raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object,
        parse_constant=_constant, parse_float=_finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError()
    # Escaped unpaired surrogates are not Unicode scalar values either.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            item.encode("utf-8", errors="strict")
    return value


def _request(method: str, url: str, headers: Mapping[str, str],
             params: Mapping[str, str | int], body: Mapping[str, str] | None,
             follow_redirects: bool) -> httpx.Request:
    if follow_redirects is not False or method not in {"GET", "POST"}:
        raise ValueError()
    if not isinstance(url, str) or _url(url) != url:
        raise ValueError()
    path = urlsplit(url).path
    projects = re.search(r"/api/v2/projects(?:/[a-z0-9][a-z0-9._-]{0,63})?$", path)
    # S3 read-only endpoints (server-design §3.3–3.4; I11): GET only, and each
    # with exactly its own query vocabulary. Never a room, WS, put or create.
    snapshot = re.search(r"/api/v2/projects/[a-z0-9][a-z0-9._-]{0,63}/snapshot$", path)
    resolve = path.endswith("/api/v2/legacy-spaces/resolve")
    health_path = path.endswith("/healthz")
    if not (health_path or path.endswith(("/api/v1/me", "/api/v2/me")) or projects
            or snapshot or resolve):
        raise ValueError()
    if snapshot or resolve:
        if method != "GET" or body is not None:
            raise ValueError()
        keys = set(params)
        if snapshot:
            # First page (optionally epoch-bound) or a fully bound continuation.
            if keys not in ({"limit"}, {"limit", "expected_epoch"},
                            {"limit", "after", "expected_seq", "expected_epoch"}):
                raise ValueError()
        elif keys != {"name"} or not isinstance(params["name"], str):
            raise ValueError()
    if method == "POST":
        if not path.endswith("/api/v2/projects") or params or body is None:
            raise ValueError()
    elif body is not None:
        raise ValueError()
    # Never forward arbitrary ambient headers, cookies, proxy auth or hooks.
    clean = {}
    for key, value in headers.items():
        name = key.lower()
        if name in clean or name not in {"accept", "cache-control", "authorization"}:
            raise ValueError()
        if not isinstance(value, str):
            raise ValueError()
        clean[name] = value
    auth = clean.get("authorization")
    # Prefixes are arbitrary: /api/v2/projects can be a deployment prefix,
    # and healthz is also a valid project ID. The guarded caller supplies auth
    # intent; do not misclassify either legitimate overlapping URL spelling.
    health = health_path and (not projects or auth is None)
    if health:
        if auth is not None or method != "GET" or params:
            raise ValueError()
    elif (not isinstance(auth, str) or not auth.startswith("Bearer ")
          or not auth[7:] or not auth.isascii()
          or any(ord(c) <= 32 or ord(c) == 127 for c in auth[7:])):
        raise ValueError()
    if any(not isinstance(k, str) or type(v) not in {str, int}
           for k, v in params.items()):
        raise ValueError()
    clean.update({"accept": "application/json", "cache-control": "no-store",
                  "accept-encoding": "identity"})
    request = httpx.Request(method, url, headers=clean, params=params, json=body,
                            extensions={"timeout": dict(_PHASE_TIMEOUTS)})
    # Fail closed rather than letting HTTPX repair a different endpoint/prefix.
    # Query values are separately encoded by HTTPX, never concatenated to a URL.
    if str(request.url.copy_with(query=None)) != url:
        raise ValueError()
    return request


async def _body(response: httpx.Response) -> dict:
    content_type = response.headers.get_list("content-type")
    if (len(content_type) != 1 or re.fullmatch(
            r'application/json\s*(?:;\s*charset\s*=\s*(?:utf-8|"utf-8")\s*)?',
            content_type[0], flags=re.IGNORECASE) is None):
        raise ValueError()
    encoding = response.headers.get_list("content-encoding")
    if encoding and (len(encoding) != 1 or encoding[0].strip().lower() != "identity"):
        raise ValueError()
    lengths = response.headers.get_list("content-length")
    declared = None
    if lengths:
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            raise ValueError()
        declared = int(lengths[0])
        if declared > _MAX_RESPONSE_BYTES:
            raise ValueError()
    raw = bytearray()
    # The low-level stream is raw (no HTTPX compression decoder). Do not use
    # aiter_raw's automatic, cancellable aclose: it marks the response closed
    # before awaiting stream cleanup. We own one shielded close in finally.
    async for chunk in response.stream:
        if len(chunk) > _MAX_RESPONSE_BYTES - len(raw):
            raise ValueError()
        raw.extend(chunk)
    if declared is not None and declared != len(raw):
        raise ValueError()
    return _decode(raw)


async def _close(resource: object, budget: float) -> tuple[bool, bool]:
    """Shield caller cancellation, but never shield our own cleanup deadline.

    A broken/slow close is a transport failure, not permission to reuse the pool.
    No background close task is abandoned. As with all asyncio deadlines, the
    trusted resource implementation must cooperate with task cancellation.
    """
    async def close() -> bool:
        try:
            async with asyncio.timeout(budget):
                await resource.aclose()
        except (Exception, asyncio.CancelledError):
            return False
        return True

    task = asyncio.create_task(close())
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled, not task.result()


class CatalogHttpTransport:
    """Dedicated low-level HTTPX pool, owned by an eventual application owner.

    Intentionally no AsyncClient: no cookie jar (including Set-Cookie reuse),
    netrc/auth, event hooks, automatic redirects or request-summary logger exists.
    ``transport`` is a trusted low-level test seam, never a browser/API option.
    Call aclose only after stopping admission and draining in-flight operations.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        _protect_logs()
        self._closed = False
        self._usable = True
        self._transport = None
        failed = False
        try:
            self._transport = transport if transport is not None else httpx.AsyncHTTPTransport(
                verify=True, trust_env=False, retries=0, http1=True, http2=False,
            )
        except Exception:
            failed = True
        if failed:
            raise CatalogError("transport_unavailable")

    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      params: Mapping[str, str | int], json: Mapping[str, str] | None,
                      follow_redirects: bool) -> CatalogResponse:
        code = "transport_unavailable"
        cancelled = False
        result = response = None
        private = _PRIVATE_IO.set(True)
        # Reserve cleanup time *within* the total deadline: a slow close cannot
        # turn a 30s request into an unbounded shutdown/drain. The normal network
        # budget is 25s and cleanup is at most 5s. Phase limits are unchanged.
        cleanup_budget = min(_CLEANUP_TIMEOUT, _TOTAL_TIMEOUT / 6)
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT - cleanup_budget):
                if self._closed or not self._usable:
                    raise ValueError()
                code = "invalid_request"
                request = _request(method, url, headers, params, json, follow_redirects)
                code = "transport_unavailable"
                # Exactly one send; no client redirect/auth/retry machinery.
                response = await self._transport.handle_async_request(request)
                code = "invalid_response"
                if 300 <= response.status_code <= 399:
                    code = "redirect_refused"
                else:
                    body = await _body(response)
                    result = CatalogResponse(response.status_code, body)
        except asyncio.CancelledError:
            cancelled = True
        except (httpx.HTTPError, TimeoutError):
            code = "transport_unavailable"
        except Exception:
            pass
        finally:
            if response is not None:
                close_cancel, close_failed = await _close(response, cleanup_budget)
                cancelled |= close_cancel
                if close_failed:
                    self._usable = False
                    result = None
                    code = "transport_unavailable"
            _PRIVATE_IO.reset(private)
        # Outside handlers: __context__/__cause__ cannot retain raw errors, even
        # CancelledError messages or asyncio.timeout's chained TimeoutError.
        if cancelled:
            raise asyncio.CancelledError() from None
        if result is None:
            raise CatalogError(code)
        return result

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        private = _PRIVATE_IO.set(True)
        try:
            cancelled, failed = await _close(self._transport, _CLEANUP_TIMEOUT)
        finally:
            _PRIVATE_IO.reset(private)
        if cancelled:
            raise asyncio.CancelledError() from None
        if failed:
            raise CatalogError("transport_unavailable")

    async def __aenter__(self) -> CatalogHttpTransport:
        if self._closed:
            raise CatalogError("transport_unavailable")
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

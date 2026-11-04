"""Per-run transport from Codex's stdio MCP client to live Studio tools (§10.6).

Only bytes cross the loopback connection: the SDK server and its member/task-bound
handlers stay in the Studio process, with the same routing checks as Claude sessions.
The listener requires a fresh secret and closes with the run, including cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
TOKEN_ENV = "VOXARIUM_STUDIO_MCP_BRIDGE_TOKEN"


@asynccontextmanager
async def bridge_servers(
    servers: dict[str, Any], max_message_bytes: int
) -> AsyncIterator[dict[str, Any]]:
    """Replace SDK entries with stdio configs; leave project servers alone."""
    async with AsyncExitStack() as stack:
        translated = dict(servers)
        for name, server in servers.items():
            if isinstance(server, dict) and server.get("type") == "sdk":
                translated[name] = await stack.enter_async_context(
                    _serve(server["instance"], max_message_bytes)
                )
        yield translated


@asynccontextmanager
async def _serve(server: Any, max_message_bytes: int) -> AsyncIterator[dict[str, Any]]:
    # Optional teams dependencies: a Codex run without Studio tools needs neither.
    import anyio
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    token = secrets.token_hex(32)
    clients: dict[asyncio.Task[Any], asyncio.StreamWriter] = {}
    closing = False

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        current = asyncio.current_task()
        assert current is not None
        try:
            supplied = await asyncio.wait_for(reader.readexactly(65), timeout=5)
            if not secrets.compare_digest(supplied, (token + "\n").encode("ascii")):
                return
            incoming, read_stream = anyio.create_memory_object_stream(0)
            write_stream, outgoing = anyio.create_memory_object_stream(0)

            async def receive() -> None:
                async with incoming:
                    while True:
                        try:
                            raw = await reader.readuntil(b"\n")
                        except asyncio.IncompleteReadError:
                            return
                        await incoming.send(SessionMessage(JSONRPCMessage.model_validate_json(raw)))

            async def send() -> None:
                async with outgoing:
                    async for message in outgoing:
                        body = message.message.model_dump_json(by_alias=True, exclude_none=True)
                        writer.write((body + "\n").encode("utf-8"))
                        await writer.drain()

            async with read_stream, write_stream, anyio.create_task_group() as group:
                group.start_soon(receive)
                group.start_soon(send)
                await server.run(read_stream, write_stream, server.create_initialization_options())
                group.cancel_scope.cancel()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        except Exception:
            log.exception("Studio MCP bridge connection failed")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            clients.pop(current, None)

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if closing:
            writer.close()
            return
        client = asyncio.create_task(handle(reader, writer))
        clients[client] = writer
        client.add_done_callback(lambda task: clients.pop(task, None))

    listener = await asyncio.start_server(
        accept, "127.0.0.1", 0, limit=max(max_message_bytes, 64 * 1024)
    )
    port = listener.sockets[0].getsockname()[1]
    try:
        yield {
            "command": sys.executable,
            "args": ["-u", str(Path(__file__).with_name("mcp_proxy.py")), str(port)],
            "env": {TOKEN_ENV: token},
        }
    finally:
        closing = True
        listener.close()
        pending = dict(clients)
        for client, writer in pending.items():
            writer.close()
            client.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        # Python 3.12 waits for accepted connections too: close those before joining.
        await listener.wait_closed()

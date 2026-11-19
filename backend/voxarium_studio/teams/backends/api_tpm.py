"""Process-wide per-connection TPM reservations for OpenAI-compatible requests (§10.6)."""
import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass

from .api_context import ContextBudgetError


@dataclass
class Reservation:
    expires: float
    tokens: int


class TokenWindow:
    def __init__(self):
        self.entries = deque()
        self.queue = asyncio.Lock()
        self.changed = asyncio.Event()
        self.cooldown_until = 0.0

    def notify(self):
        self.changed.set()
        self.changed = asyncio.Event()

    def cooldown(self):
        self.cooldown_until = time.monotonic() + 60
        self.notify()

    async def reserve(self, tokens, limit):
        if tokens > limit:
            raise ContextBudgetError(
                f"This request needs a conservative reservation of {tokens:,} tokens, above the "
                f"configured TPM of {limit:,}. Shorten the input or increase this connection's TPM."
            )
        # FIFO admission prevents small tool calls from starving a larger waiting task.
        async with self.queue:
            while True:
                now = time.monotonic()
                while self.entries and self.entries[0].expires <= now:
                    self.entries.popleft()
                available = limit - sum(entry.tokens for entry in self.entries)
                if now >= self.cooldown_until and tokens <= available:
                    entry = Reservation(now + 60, tokens)
                    self.entries.append(entry)
                    return entry
                wake_at = self.cooldown_until
                if tokens > available:
                    wake_at = max(wake_at, self.entries[0].expires)
                try:
                    await asyncio.wait_for(self.changed.wait(), wake_at - now)
                except TimeoutError:
                    pass

    async def reserve_request(self, payload, limit, output_limit):
        # UTF-8 bytes conservatively bound byte-tokenized text without requiring the
        # tokenizer of the remote model. Include tool schemas and protocol framing.
        size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return await self.reserve(size + output_limit, limit)

    def observe(self, entry, usage, output_limit):
        prompt = usage.get("prompt_tokens")
        if type(prompt) is int and prompt > 0:
            # Keep the requested output allowance even if the provider finishes early.
            entry.tokens = prompt + output_limit
            self.notify()


_windows: dict[str, TokenWindow] = {}


def connection_window(connection):
    if not connection.use_limits:
        return None
    if connection.id not in _windows:
        _windows[connection.id] = TokenWindow()
    return _windows[connection.id]

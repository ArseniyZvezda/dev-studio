"""API-equivalent token cost for Codex runs (§10.6), never a subscription bill.

Standard USD / million tokens, checked 2026-10-26 against
https://developers.openai.com/api/docs/pricing and the linked model pages.
The CLI reports aggregate usage, so this estimate excludes tool fees, regional premiums,
service tiers and per-request long-context premiums. Unknown models stay unpriced.
"""

from __future__ import annotations

import re

# Input, cached input, output. Keep exact names: a new model needs a verified tariff.
_RATES: dict[str, tuple[float, float, float]] = {
    "gpt-6.1-sol": (2.0, 0.10, 10.0),
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-6-sol": (2.0, 0.20, 10.0),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.0, 0.40, 20.0),
    "gpt-5.6-terra": (2.0, 0.20, 12.0),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.3-codex": (1.75, 0.175, 14.0),
}


def estimate_cost(model: str | None, usage: dict[str, int]) -> float | None:
    """Price Studio usage, whose input count excludes the separately counted cache reads."""
    name = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model or "")
    rates = _RATES.get(name)
    if rates is None or not usage:
        return None
    return sum(
        max(0, usage.get(key, 0)) * rate
        for key, rate in zip(
            ("input_tokens", "cache_read_input_tokens", "output_tokens"), rates
        )
    ) / 1_000_000

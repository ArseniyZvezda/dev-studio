"""The magic wand: which pixels are background, and how hard the edge between them is.

Pure standard library, and pure *byte-string* work — which is the whole performance story.
A 4096-square texture is 16 million pixels, so anything with a Python loop per pixel would
turn a node into a coffee break. Every step below is either a 256-entry
:meth:`bytes.translate` (the colour distance, the tolerance ramp), a strided slice (the
channel planes), a C-level :func:`map` over whole planes (the blur), or a
:meth:`bytes.find` (the flood fill, which scans *runs* rather than pixels). The only Python
loop that runs per-something is the flood fill's, and it iterates spans.

Distance is the largest difference on any single channel — Chebyshev, the same measure a
magic wand in an image editor uses — because it is what makes the tolerance a 256-entry
lookup table rather than a square root per pixel. It also behaves the way people expect:
one channel being far off is enough to make a colour "different", which a Euclidean average
would dilute.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

REGIONS = ("edges", "everywhere")
"""Where a matching pixel counts as background. Mirrored in ``plugin.py``, which cannot
import this module — a node script runs out of process in the plugin's own venv."""

ONE = b"\x01"

_INVERT = bytes(255 - value for value in range(256))
"""Background strength -> alpha. The two are the same number read from opposite ends."""

_NONZERO = bytes(1 if value else 0 for value in range(256))
"""Any background strength at all -> 1, so the flood fill can search for a byte value."""


@dataclass(frozen=True)
class KeyOptions:
    """What the node was configured with, already parsed and clamped."""

    key_color: tuple[int, int, int] = (255, 255, 255)
    tolerance: float = 12.0
    """Percentage of the largest possible one-channel difference, 0-100."""
    region: str = "edges"
    edge_sharpness: float = 80.0
    """100 is a hard threshold at the tolerance; 0 fades across the whole band."""
    antialias: bool = True


class KeyResult(NamedTuple):
    alpha: bytes
    removed: int
    """Pixels left fully transparent."""
    softened: int
    """Pixels left partly transparent — the edge, and what antialiasing widens."""


def parse_color(raw: object) -> tuple[int, int, int]:
    """A colour as the UI, a pipeline YAML or an upstream node might spell it.

    Deliberately strict about what it cannot read: a mistyped colour that quietly fell
    back to white would key the picture against something the user never chose, and the
    node would look like it had simply done nothing.
    """
    if isinstance(raw, (list, tuple)) and len(raw) >= 3:
        return tuple(_channel(value) for value in raw[:3])  # type: ignore[return-value]

    text = str(raw or "").strip().lower()
    if text.startswith("rgb(") and text.endswith(")"):
        parts = [part.strip() for part in text[4:-1].split(",")]
        if len(parts) < 3:
            raise ValueError(f"colour {raw!r} needs three channels")
        return tuple(_channel(part) for part in parts[:3])  # type: ignore[return-value]

    digits = text[1:] if text.startswith("#") else text
    if len(digits) == 3:  # #rgb, the CSS shorthand
        digits = "".join(digit * 2 for digit in digits)
    if len(digits) == 8:  # #rrggbbaa — the alpha is not a colour to key against
        digits = digits[:6]
    if len(digits) != 6 or any(digit not in "0123456789abcdef" for digit in digits):
        raise ValueError(f"colour {raw!r} is not a hex colour like #ffffff")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _channel(value: object) -> int:
    number = int(round(float(value)))
    return 0 if number < 0 else 255 if number > 255 else number


def clamp_percent(value: object, fallback: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return 0.0 if number < 0 else 100.0 if number > 100 else number


@lru_cache(maxsize=8)
def _delta_table(reference: int) -> bytes:
    """|sample - reference| for every possible sample, as a translation table."""
    return bytes(abs(value - reference) for value in range(256))


def difference(pixels: bytes | bytearray, key: tuple[int, int, int]) -> bytes:
    """Per-pixel distance from `key`: the largest difference on any one channel.

    Three strided slices pull the colour planes out at C speed, three `translate` calls
    turn each into a distance plane, and `map(max, ...)` folds them — a builtin over three
    byte strings, so no Python-level work happens per pixel.
    """
    red = pixels[0::4].translate(_delta_table(key[0]))
    green = pixels[1::4].translate(_delta_table(key[1]))
    blue = pixels[2::4].translate(_delta_table(key[2]))
    return bytes(map(max, red, green, blue))


def strength_lut(tolerance: float, sharpness: float) -> bytes:
    """Distance -> how much of a background pixel this is, 0-255.

    The tolerance is the outer edge of the band; the sharpness decides where inside it the
    fade begins. At 100 the two coincide and the cut is a hard threshold — the classic
    wand, and the jagged edge antialiasing then softens. Lower it and colours *approaching*
    the key go partly transparent instead, which is what keeps a soft shadow or a blurred
    outline from being either cut off square or left as a halo.
    """
    outer = 255.0 * max(0.0, min(100.0, tolerance)) / 100.0
    inner = outer * max(0.0, min(100.0, sharpness)) / 100.0
    span = outer - inner

    table = bytearray(256)
    for distance in range(256):
        if distance <= inner:
            table[distance] = 255
        elif distance >= outer:
            table[distance] = 0
        else:
            table[distance] = round(255.0 * (outer - distance) / span)
    return bytes(table)


def background_strength(
    diff: bytes, width: int, height: int, lut: bytes, region: str
) -> bytes:
    """How much of each pixel the key claims, before the source's own alpha has a say."""
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; expected one of {list(REGIONS)}")
    strength = diff.translate(lut)
    if region == "everywhere":
        return strength
    reachable = flood_from_border(strength.translate(_NONZERO), width, height)
    # 0/1 * strength: the ramp survives inside the flooded area and is erased outside it.
    return bytes(map(_mul, strength, reachable))


def _mul(a: int, b: int) -> int:
    return a * b


def flood_from_border(candidate: bytes, width: int, height: int) -> bytearray:
    """Which candidate pixels the image border can reach, four-connected.

    A span fill rather than a pixel fill, and `bytes.find` is why it is fast: finding where
    a run of candidates ends, or where the next one begins on the row above, is a C-level
    scan. The Python loop below therefore turns once per *span*, not once per pixel — on
    the flat backdrop this node exists for, that is a few thousand iterations for a few
    million pixels.
    """
    size = width * height
    filled = bytearray(size)
    if size == 0:
        return filled

    stack: list[int] = []
    last_row = size - width
    for x in range(width):
        if candidate[x]:
            stack.append(x)
        if candidate[last_row + x]:
            stack.append(last_row + x)
    for base in range(0, size, width):
        if candidate[base]:
            stack.append(base)
        if candidate[base + width - 1]:
            stack.append(base + width - 1)

    while stack:
        seed = stack.pop()
        if filled[seed] or not candidate[seed]:
            continue

        base = seed - seed % width
        row_end = base + width
        # Bounded to this row on both sides, so a run never leaks into the row before it.
        left = candidate.rfind(0, base, seed)
        left = base if left < 0 else left + 1
        right = candidate.find(0, seed, row_end)
        if right < 0:
            right = row_end
        filled[left:right] = ONE * (right - left)

        for neighbour in (base - width, base + width):
            if neighbour < 0 or neighbour >= size:
                continue
            cursor = neighbour + (left - base)
            stop = neighbour + (right - base)
            while cursor < stop:
                run = candidate.find(1, cursor, stop)
                if run < 0:
                    break
                # One seed per run: popping it expands the whole run in one step.
                if not filled[run]:
                    stack.append(run)
                gap = candidate.find(0, run, stop)
                cursor = stop if gap < 0 else gap
    return filled


def _tent(left: int, middle: int, right: int) -> int:
    """A 1-2-1 tap, rounded. Three of these make the 3x3 kernel below."""
    return (left + 2 * middle + right + 2) >> 2


def blur3(plane: bytes, width: int, height: int) -> bytes:
    """A separable 3x3 tent blur over one byte plane, with the borders clamped.

    Applied to alpha, this *is* the antialiasing. Nothing needs to detect where the edge
    is: blurring a constant region gives the same constant back, so only pixels that
    already have a transition next to them can change, and a staircase between 0 and 255
    comes out as the ramp the same edge would have had if it had been rendered rather than
    thresholded.
    """
    if width <= 0 or height <= 0:
        return plane

    rows = []
    for base in range(0, width * height, width):
        row = plane[base : base + width]
        rows.append(bytes(map(_tent, row[:1] + row[:-1], row, row[1:] + row[-1:])))
    horizontal = b"".join(rows)

    above = horizontal[:width] + horizontal[:-width]
    below = horizontal[width:] + horizontal[-width:]
    return bytes(map(_tent, above, horizontal, below))


def key_out(pixels: bytes | bytearray, width: int, height: int, options: KeyOptions) -> KeyResult:
    """The whole cut, as one alpha plane.

    The source's own alpha is intersected *after* the blur, not before: smoothing the two
    together would soften edges this node never cut, and a picture that already had a
    clean alpha channel would come back very slightly eaten at every outline.
    """
    diff = difference(pixels, options.key_color)
    lut = strength_lut(options.tolerance, options.edge_sharpness)
    strength = background_strength(diff, width, height, lut, options.region)

    alpha = strength.translate(_INVERT)
    if options.antialias:
        alpha = blur3(alpha, width, height)

    source = bytes(pixels[3::4])
    if source.count(255) != len(source):
        alpha = bytes(map(min, source, alpha))

    removed = alpha.count(0)
    opaque = alpha.count(255)
    return KeyResult(alpha=alpha, removed=removed, softened=len(alpha) - removed - opaque)

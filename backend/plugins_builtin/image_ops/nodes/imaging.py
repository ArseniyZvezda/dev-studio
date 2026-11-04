"""An image in, a PNG out — RGBA8 in between.

Two decoders, and which one runs is decided by what is installed rather than by the file.
**Pillow wins whenever it is importable**, because it is faster and because it is the only
one here that reads JPEG, WebP, BMP, TGA or TIFF at all. The zlib decoder below is the
floor: PNG with no dependencies, so "Remove Background" works the moment the plugin is
enabled, and keeps working on its own output before anyone builds the venv.

Preferring Pillow costs nothing in reproducibility, which is worth stating because the
sibling ``hunyuan3d/nodes/imaging.py`` reaches the opposite conclusion. PNG decoding is
defined bit-exactly by the format — filters, palettes and bit depths all have one right
answer — so both paths here produce the same pixels by construction. A JPEG has no such
guarantee (two IDCTs need not agree in the last bit), which is why that module refuses to
let Pillow near one, and why this module never decodes a JPEG itself.

Everything is *straight* alpha: a keyed-out pixel keeps the colour it had. Premultiplying
would fold the background back into the very pixels the node was asked to remove.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from operator import add as _add
from pathlib import Path
from typing import Any

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
"""PNG colour types: greyscale, truecolour, indexed, greyscale+alpha, truecolour+alpha."""

CONTENT = b"\x01"
"""What `alpha_bounds` translates an above-threshold pixel to, so a bounding box
falls out of `bytes.find` rather than out of a scan over pixels."""

OPAQUE = b"\xff"


MEDIAN_SAMPLES = 16
"""Most source pixels `resample_median` looks at per axis of one output pixel's footprint.

A shrink to a tenth means footprints of a hundred pixels and a shrink to a hundredth means
ten thousand, and the median of a few hundred well-spread samples is the median. The cap is
what keeps an extreme reduction from costing the whole source, and it is a regular grid
rather than a sample, so two runs of the same node still agree (§8.2)."""


class ImageError(ValueError):
    """A file that cannot be turned into pixels here, with a reason a user can act on."""


def _spans(source: int, target: int, samples: int) -> list[tuple[int, int, int]]:
    """Per output index, the ``(start, stop, step)`` of source indices it covers.

    `stop` is forced past `start` so every output pixel has at least one source pixel to
    take a median of, and `step` thins a footprint wider than `samples` down to it.
    """
    out = []
    for index in range(target):
        start = index * source // target
        stop = max(start + 1, (index + 1) * source // target)
        out.append((start, stop, max(1, -(-(stop - start) // samples))))
    return out


def _median_pixel(reds, greens, blues, alphas) -> bytes:
    """One RGBA8 pixel: a footprint's median colour, taken whole, over its median alpha.

    **The colour is selected, not computed**, and that is the difference that matters. A
    per-channel median is the textbook median filter and it recombines: the middle red, the
    middle green and the middle blue need not have belonged to the same pixel, so a footprint
    straddling an orange body and a blue outline can answer with a pink that is in neither.
    Ranking the footprint by ``r + g + b`` and taking that pixel *entire* keeps the answer a
    colour the picture actually contained — so the output's palette is a subset of the
    input's, which is what anything downstream that quantises (a `.vox`, an indexed PNG,
    pixel art) needs, and what an average could never give.

    Ties rank by first occurrence, so the same footprint always answers the same way.

    Alpha is still medianed over *all* the pixels, separately, because coverage is not
    colour: a footprint that is nine parts transparent should come out nearly transparent
    whichever pixel lends it its colour. The colour is chosen among the pixels that draw
    something — straight alpha means a keyed-out pixel still carries the background's colour,
    and letting those rank would put the colour the key removed back on the subject's edge.

    `bytearray.count(0)` is the whole fast path: it answers "is this footprint mixed?" at C
    speed, and only a mixed one — the subject's outline, never its interior — is walked in
    Python.
    """
    count = len(alphas)
    alpha = sorted(alphas)[count // 2]

    blank = alphas.count(0)
    if blank == 0 or blank == count:
        # `map(add, map(add, …))` rather than a comprehension: two C-level passes, where a
        # three-argument lambda would call back into Python once per sample.
        totals = list(map(_add, map(_add, reds, greens), blues))
        chosen = totals.index(sorted(totals)[count // 2])
    else:
        drawn = [index for index, value in enumerate(alphas) if value]
        totals = [reds[index] + greens[index] + blues[index] for index in drawn]
        chosen = drawn[totals.index(sorted(totals)[len(drawn) // 2])]

    return bytes((reds[chosen], greens[chosen], blues[chosen], alpha))


class Raster:
    """An RGBA8 bitmap, row-major, top row first — the way PNG stores one."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int, pixels: bytearray) -> None:
        expected = width * height * 4
        if len(pixels) != expected:
            raise ImageError(f"{width}x{height} needs {expected} bytes, got {len(pixels)}")
        self.width = width
        self.height = height
        self.pixels = pixels

    @property
    def count(self) -> int:
        """Pixels, not bytes."""
        return self.width * self.height

    def alpha(self) -> bytes:
        return bytes(self.pixels[3::4])

    def crop(self, left: int, top: int, width: int, height: int) -> Raster:
        """The sub-rectangle at (*left*, *top*), copied out as a raster of its own.

        One C-level slice per row rather than a loop over pixels, so cutting a 4096-square
        texture into quarters costs a few thousand `memcpy`s instead of sixteen million
        Python-level moves.
        """
        if width <= 0 or height <= 0:
            raise ImageError(f"a crop needs a positive size, got {width}x{height}")
        if left < 0 or top < 0 or left + width > self.width or top + height > self.height:
            raise ImageError(
                f"crop {width}x{height} at ({left},{top}) falls outside "
                f"a {self.width}x{self.height} image"
            )
        stride = self.width * 4
        row = width * 4
        out = bytearray(row * height)
        for y in range(height):
            start = (top + y) * stride + left * 4
            out[y * row : (y + 1) * row] = self.pixels[start : start + row]
        return Raster(width, height, out)

    def alpha_bounds(self, cutoff: int = 0) -> tuple[int, int, int, int] | None:
        """``(left, top, width, height)`` around every pixel whose alpha is above *cutoff*.

        None when no pixel is: a fully transparent image has no bounding box, and there is
        no rectangle worth inventing for it.

        C-level scans rather than a loop over pixels. The alpha plane is translated to one
        flag byte per pixel; `find` and `rfind` over the whole plane give the first and last
        rows holding anything, and only those rows are then scanned for the extreme columns
        — each with its search window already narrowed to what would have to beat the best
        edge so far. A 4096-square texture costs a few thousand C calls rather than sixteen
        million Python-level comparisons.
        """
        table = bytes(0 if value <= cutoff else 1 for value in range(256))
        mask = self.alpha().translate(table)

        first = mask.find(CONTENT)
        if first < 0:
            return None
        top = first // self.width
        bottom = mask.rfind(CONTENT) // self.width

        left, right = self.width, -1
        for y in range(top, bottom + 1):
            row = mask[y * self.width : (y + 1) * self.width]
            found = row.find(CONTENT, 0, left)
            if found >= 0:
                left = found
            found = row.rfind(CONTENT, right + 1)
            if found >= 0:
                right = found
            if left == 0 and right == self.width - 1:
                break  # the box already spans the image; no later row can widen it
        return left, top, right - left + 1, bottom - top + 1

    def window(self, left: int, top: int, width: int, height: int) -> Raster:
        """The *width* x *height* rectangle at (*left*, *top*), transparent where it falls
        outside the image.

        The permissive counterpart to :meth:`crop`, and both exist deliberately. A quadrant
        cut has to *refuse* a rectangle that escapes, because four tiles which silently
        shrank stop reassembling; a fixed output resolution is asking for a frame that may
        well be larger than the picture, and padding is the answer it wants rather than an
        error.

        The padding is (0, 0, 0, 0) — transparent black, straight alpha like everything else
        here, so a padded pixel contributes no colour when the result is composited.
        """
        if width <= 0 or height <= 0:
            raise ImageError(f"a window needs a positive size, got {width}x{height}")
        out = bytearray(width * height * 4)

        start_x, start_y = max(left, 0), max(top, 0)
        end_x, end_y = min(left + width, self.width), min(top + height, self.height)
        if end_x <= start_x or end_y <= start_y:
            return Raster(width, height, out)  # the frame missed the picture entirely

        stride, out_stride = self.width * 4, width * 4
        row = (end_x - start_x) * 4
        offset = (start_x - left) * 4
        for y in range(start_y, end_y):
            source = y * stride + start_x * 4
            target = (y - top) * out_stride + offset
            out[target : target + row] = self.pixels[source : source + row]
        return Raster(width, height, out)

    def resample_median(self, width: int, height: int, *, samples: int = MEDIAN_SAMPLES) -> Raster:
        """Shrink to *width* x *height*, each output pixel the **median** of the source
        pixels it covers.

        Median rather than the usual box average, and the difference is the whole reason to
        write a resampler here rather than reach for one: an average of two colours is a
        third colour that was never in the picture, and on a keyed sprite that third colour
        is a rim of half-background all the way round. A median cannot produce that rim and
        leaves a hard edge hard, which is what pixel art, a sprite sheet and anything
        downstream that quantises actually want.

        The colour is **selected whole** rather than medianed channel by channel — see
        `_median_pixel`, which is where that distinction is argued. Alpha is medianed
        separately over the whole footprint, and an even count takes the upper of the two
        middle samples rather than averaging them, for the same reason the median was chosen
        at all.

        Reduction only — `frame_for`'s framing never asks for an enlargement, and a median
        has nothing to say about a pixel it would have to invent. A footprint is thinned to
        at most *samples* per axis, so a hundred-fold shrink medians a regular grid of a few
        hundred of its source pixels rather than all ten thousand: deterministic, so the
        §8.2 cache key still describes the result, and a fraction of the cost.
        """
        if width <= 0 or height <= 0:
            raise ImageError(f"a resample needs a positive size, got {width}x{height}")
        if width > self.width or height > self.height:
            raise ImageError(
                f"resample_median reduces only, and {self.width}x{self.height} -> "
                f"{width}x{height} is an enlargement"
            )
        if (width, height) == (self.width, self.height):
            return Raster(width, height, bytearray(self.pixels))

        rows = _spans(self.height, height, samples)
        columns = _spans(self.width, width, samples)

        pixels = self.pixels
        stride = self.width * 4
        out = bytearray(width * height * 4)
        cursor = 0
        for top, bottom, row_step in rows:
            bases = [y * stride for y in range(top, bottom, row_step)]
            for left, right, column_step in columns:
                first, last, gap = left * 4, right * 4, column_step * 4
                # Four strided slices per source row rather than a walk over pixels: the
                # channel planes come out at C speed and only the medians are Python's work.
                if len(bases) == 1:
                    base = bases[0]
                    reds = pixels[base + first : base + last : gap]
                    greens = pixels[base + first + 1 : base + last : gap]
                    blues = pixels[base + first + 2 : base + last : gap]
                    alphas = pixels[base + first + 3 : base + last : gap]
                else:
                    reds, greens, blues, alphas = (
                        bytearray(),
                        bytearray(),
                        bytearray(),
                        bytearray(),
                    )
                    for base in bases:
                        reds += pixels[base + first : base + last : gap]
                        greens += pixels[base + first + 1 : base + last : gap]
                        blues += pixels[base + first + 2 : base + last : gap]
                        alphas += pixels[base + first + 3 : base + last : gap]
                out[cursor : cursor + 4] = _median_pixel(reds, greens, blues, alphas)
                cursor += 4
        return Raster(width, height, out)

    def with_alpha(self, alpha: bytes) -> Raster:
        """A copy carrying `alpha`, one byte per pixel, over the same colours."""
        if len(alpha) != self.count:
            raise ImageError(f"alpha is {len(alpha)} bytes for {self.count} pixels")
        pixels = bytearray(self.pixels)
        pixels[3::4] = alpha
        return Raster(self.width, self.height, pixels)


# --------------------------------------------------------------------------- reading


def pillow():
    """Pillow's ``Image`` if this environment has it, else None — never an import error."""
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def read(path: Path) -> Raster:
    """Decode a file into RGBA8, saying what would fix it when nothing here can."""
    data = path.read_bytes()
    if not data:
        raise ImageError(f"{path.name} is empty")

    image_module = pillow()
    if image_module is not None:
        return _decode_with_pillow(data, image_module, path.name)

    if data.startswith(PNG_MAGIC):
        return decode_png(data)

    raise ImageError(
        f"{path.name} is not a PNG, and PNG is the only format this plugin reads on its "
        "own. Build the image_ops environment (Plugins -> Build environment) to add JPEG, "
        "WebP, BMP, TGA and TIFF."
    )


def _decode_with_pillow(data: bytes, image_module, name: str) -> Raster:
    from PIL import ImageOps

    try:
        with image_module.open(io.BytesIO(data)) as opened:
            # The browser honours EXIF orientation when it draws the source on the node, so
            # a photo whose camera stored it sideways has to be rotated here too — or the
            # cutout comes back in an orientation the user never saw.
            upright = ImageOps.exif_transpose(opened) or opened
            rgba = upright.convert("RGBA")
            return Raster(rgba.width, rgba.height, bytearray(rgba.tobytes()))
    except ImageError:
        raise
    except Exception as cause:  # Pillow raises a family of its own, all meaning this
        raise ImageError(f"{name} could not be decoded: {cause}") from cause


def decode_png(data: bytes) -> Raster:
    """PNG -> RGBA8 on zlib alone. Every colour type and bit depth except Adam7."""
    if not data.startswith(PNG_MAGIC):
        raise ImageError("not a PNG")

    header: tuple[int, ...] | None = None
    palette = b""
    transparency = b""
    idat = bytearray()

    offset = len(PNG_MAGIC)
    while offset + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, offset)
        kind = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        offset += 12 + length  # length, type, body, CRC
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"tRNS":
            transparency = body
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break

    if header is None:
        raise ImageError("PNG has no header chunk")
    width, height, depth, colour, compression, filtering, interlace = header
    if width <= 0 or height <= 0:
        raise ImageError(f"PNG has no pixels ({width}x{height})")
    if compression != 0 or filtering != 0:
        raise ImageError("PNG uses a compression or filter method the format never defined")
    if interlace:
        raise ImageError("interlaced PNG — build the image_ops environment, Pillow reads these")
    if colour not in _CHANNELS:
        raise ImageError(f"unknown PNG colour type {colour}")
    if colour == 3 and not palette:
        raise ImageError("indexed PNG with no palette")

    channels = _CHANNELS[colour]
    raw = _unfilter(zlib.decompress(bytes(idat)), width, height, channels, depth)
    pixels = _to_rgba(raw, width, height, channels, depth, colour, palette, transparency)
    return Raster(width, height, pixels)


def _unfilter(raw: bytes, width: int, height: int, channels: int, depth: int) -> bytearray:
    """Undo the per-row filters, leaving the scanlines packed end to end.

    Filter 0 is the whole row at C speed and filter 2 is one `map` over two byte strings;
    the other three are genuinely sequential — byte N needs byte N-bpp — so they cost a
    Python loop. That is the reason Pillow is preferred above wherever it exists, rather
    than a reason to leave a format unreadable without it.
    """
    stride = (width * channels * depth + 7) // 8
    bpp = max(1, channels * depth // 8)
    expected = (stride + 1) * height
    if len(raw) < expected:
        raise ImageError(f"PNG is truncated: {len(raw)} bytes of pixels, expected {expected}")

    out = bytearray(stride * height)
    previous = bytes(stride)
    position = 0
    for row in range(height):
        kind = raw[position]
        line = bytearray(raw[position + 1 : position + 1 + stride])
        position += 1 + stride

        if kind == 0:  # None
            pass
        elif kind == 2:  # Up
            line = bytearray(map(_wrap_add, line, previous))
        elif kind == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                corner = previous[i - bpp] if i >= bpp else 0
                estimate = left + up - corner
                da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = up
                else:
                    nearest = corner
                line[i] = (line[i] + nearest) & 0xFF
        else:
            raise ImageError(f"unknown PNG row filter {kind}")

        start = row * stride
        out[start : start + stride] = line
        previous = bytes(line)
    return out


def _wrap_add(a: int, b: int) -> int:
    return (a + b) & 0xFF


def _wrap_sub(a: int, b: int) -> int:
    return (a - b) & 0xFF


def _to_rgba(
    raw: bytearray,
    width: int,
    height: int,
    channels: int,
    depth: int,
    colour: int,
    palette: bytes,
    transparency: bytes,
) -> bytearray:
    """Unfiltered scanlines -> RGBA8, through byte-strided slicing wherever possible.

    Every assignment below is a C-level copy: ``out[0::4] = raw[0:count*3:3]`` moves a
    million pixels with no Python loop, which is what keeps the fallback decoder usable
    rather than merely correct.
    """
    count = width * height

    if depth == 16:
        # Take the high byte of every sample. `convert("RGBA")` drops the low one too, so
        # the two decoders still agree.
        raw = bytearray(raw[0::2])
    elif depth < 8:
        raw = _expand_bits(raw, width, height, channels, depth, scale=colour != 3)

    out = bytearray(count * 4)
    if colour == 6:  # truecolour + alpha, already what we want
        return bytearray(raw[: count * 4])
    if colour == 2:  # truecolour
        out[0::4] = raw[0 : count * 3 : 3]
        out[1::4] = raw[1 : count * 3 : 3]
        out[2::4] = raw[2 : count * 3 : 3]
        out[3::4] = OPAQUE * count
        return out
    if colour == 0:  # greyscale
        grey = bytes(raw[:count])
        out[0::4] = grey
        out[1::4] = grey
        out[2::4] = grey
        out[3::4] = grey.translate(_keyed_alpha(transparency)) if transparency else OPAQUE * count
        return out
    if colour == 4:  # greyscale + alpha
        grey = bytes(raw[0 : count * 2 : 2])
        out[0::4] = grey
        out[1::4] = grey
        out[2::4] = grey
        out[3::4] = raw[1 : count * 2 : 2]
        return out

    # Indexed: four 256-byte translation tables turn the index plane into colour planes.
    red, green, blue = bytearray(256), bytearray(256), bytearray(256)
    alpha = bytearray(OPAQUE * 256)
    for index in range(min(len(palette) // 3, 256)):
        red[index] = palette[index * 3]
        green[index] = palette[index * 3 + 1]
        blue[index] = palette[index * 3 + 2]
    for index, value in enumerate(transparency[:256]):
        alpha[index] = value
    indices = bytes(raw[:count])
    out[0::4] = indices.translate(bytes(red))
    out[1::4] = indices.translate(bytes(green))
    out[2::4] = indices.translate(bytes(blue))
    out[3::4] = indices.translate(bytes(alpha))
    return out


def _keyed_alpha(transparency: bytes) -> bytes:
    """tRNS on a greyscale image: one sample value is fully transparent, the rest opaque."""
    (key,) = struct.unpack(">H", transparency[:2])
    table = bytearray(OPAQUE * 256)
    table[min(key, 255)] = 0
    return bytes(table)


def _expand_bits(
    raw: bytearray, width: int, height: int, channels: int, depth: int, *, scale: bool
) -> bytearray:
    """1/2/4-bit rows -> one byte per sample, scaling greyscale up to the full range."""
    stride = (width * channels * depth + 7) // 8
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    factor = 255 // mask
    limit = width * channels
    out = bytearray(limit * height)
    for row in range(height):
        source = raw[row * stride : (row + 1) * stride]
        target = row * limit
        produced = 0
        for byte in source:
            for slot in range(per_byte):
                if produced >= limit:
                    break
                sample = (byte >> (8 - depth * (slot + 1))) & mask
                out[target + produced] = sample * factor if scale else sample
                produced += 1
    return out


# --------------------------------------------------------------------------- writing


def write_png(raster: Raster, path: Path) -> int:
    """Write RGBA8 out as a PNG, answering with the file's size in bytes."""
    image_module = pillow()
    if image_module is not None:
        image_module.frombytes(
            "RGBA", (raster.width, raster.height), bytes(raster.pixels)
        ).save(path, format="PNG")
    else:
        path.write_bytes(encode_png(raster))
    return path.stat().st_size


def encode_png(raster: Raster) -> bytes:
    """RGBA8 -> PNG bytes, every row filtered `Up`.

    `Up` rather than none: it is one `map` over two byte strings, and it turns the flat
    regions a cutout is mostly made of into runs of zero that deflate to almost nothing.
    Paeth would compress better still and costs a Python loop per byte, which on a 4096²
    texture is the difference between a second and a minute.
    """
    stride = raster.width * 4
    rows = bytearray()
    previous = bytes(stride)
    for row in range(raster.height):
        line = bytes(raster.pixels[row * stride : (row + 1) * stride])
        rows.append(2)
        rows += bytes(map(_wrap_sub, line, previous))
        previous = line

    header = struct.pack(">IIBBBBB", raster.width, raster.height, 8, 6, 0, 0, 0)
    return b"".join(
        (
            PNG_MAGIC,
            _chunk(b"IHDR", header),
            _chunk(b"IDAT", zlib.compress(bytes(rows), 6)),
            _chunk(b"IEND", b""),
        )
    )


def _chunk(kind: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(kind + body) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)


# --------------------------------------------------------------------------- naming

CHUNK = 1 << 20


def output_name(source: Path, tag: str = "", *settings: Any) -> str:
    """What to call the file a node derives from *source* under these *settings*.

    ``<stem>-<tag>-<fingerprint>.png``, where the fingerprint is those source bytes and
    those settings together. It is in the filename because the alternative clobbers cached
    work: a fixed ``<stem>-nobg.png`` is one file that every settings change overwrites, so
    a run reusing an earlier cache entry (§8.2) would point at a path whose contents are now
    somebody else's result — the cache would be right and the picture wrong.

    Stable is the other half of it. The same bytes keyed the same way produce the same name,
    so re-running a pipeline overwrites its own output rather than piling up near-duplicates
    beside it.
    """
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    if settings:
        digest.update(repr(settings).encode("utf-8"))

    stem = "".join(ch if ch.isalnum() else "-" for ch in source.stem.lower())[:40].strip("-")
    parts = (stem or "image", tag, digest.hexdigest()[:8])
    return "-".join(part for part in parts if part) + ".png"

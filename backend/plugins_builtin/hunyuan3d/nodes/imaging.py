"""Decoding a texture image, with the stdlib where that is possible at all.

The same constraint every module in ``nodes/`` is written under: a node script runs in
the plugin's own venv, which may never have been provisioned, so nothing here may
*require* a third-party package. Both containers that matter are therefore written out
in full: PNG on :mod:`zlib`, and **baseline JPEG**, which is what Hunyuan3D's texture
pass actually bakes — 2048x2048, 4:2:0, and labelled ``image/png`` in the GLB regardless.
That mislabelling is why nothing here trusts a declared MIME type: the container is
decided by the bytes, and the declaration is only ever quoted back in an error.

The JPEG path decodes **DC coefficients only**, which is a 1/8-scale image — 256 square
out of 2048 square. That is not a shortcut taken reluctantly: the one consumer is
:mod:`voxelize`, whose grid tops out at 200 per axis, so a full-resolution decode would
spend a minute of pure-Python IDCT on detail that is averaged away in the same pass.

Pillow is used only for containers neither decoder handles (WebP, BMP, TGA), and never in
preference to them — a node whose colours depended on whether Pillow happened to be
installed would hand back different results for the same §8.2 cache key.

The result is deliberately small: one RGB8 buffer and a nearest-neighbour sampler.
Filtering it would only blur colours that are about to be quantised into a 256-entry
palette anyway.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import NamedTuple

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"

REPEAT = 10497
CLAMP_TO_EDGE = 33071
MIRRORED_REPEAT = 33648
"""glTF's sampler wrap enums, so a texture is sampled the way its file asked to be."""

SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tga": "image/x-tga",
}

_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
"""PNG colour types: greyscale, truecolour, indexed, greyscale+alpha, truecolour+alpha."""


class ImageError(ValueError):
    """An image that cannot be decoded here. Callers decide whether that is fatal."""


class Image:
    """An RGB8 bitmap, row-major, the top row first — the way both PNG and glTF UVs go."""

    __slots__ = ("width", "height", "pixels")

    def __init__(self, width: int, height: int, pixels: bytes) -> None:
        if width <= 0 or height <= 0 or len(pixels) < width * height * 3:
            raise ImageError(f"An image of {width}x{height} needs {width * height * 3} bytes")
        self.width = width
        self.height = height
        self.pixels = pixels

    def texel(self, u: float, v: float, wrap_s: int = REPEAT, wrap_t: int = REPEAT) -> tuple[int, int, int]:
        """The colour at a UV. ``v`` runs downward, which is what glTF means by (0, 0).

        ``floor`` rather than ``int``: Python truncates toward zero, which sends every
        negative UV in the first texel-width to column 0 instead of wrapping it round to
        the far edge — a seam that only appears on meshes whose unwrap runs slightly
        outside the unit square, which is most of them.
        """
        x = _wrap(math.floor(u * self.width), self.width, wrap_s)
        y = _wrap(math.floor(v * self.height), self.height, wrap_t)
        at = (y * self.width + x) * 3
        return (self.pixels[at], self.pixels[at + 1], self.pixels[at + 2])


def _wrap(value: int, size: int, mode: int) -> int:
    if mode == CLAMP_TO_EDGE:
        return 0 if value < 0 else min(value, size - 1)
    if mode == MIRRORED_REPEAT:
        period = value % (size * 2)
        return period if period < size else size * 2 - 1 - period
    return value % size


# ------------------------------------------------------------------------------ entry


def sniff(data: bytes) -> str:
    """The container these bytes actually are, whatever the file said they were."""
    if data[:8] == PNG_MAGIC:
        return "image/png"
    if data[:2] == JPEG_MAGIC:
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return ""


def decode(data: bytes, *, mime: str = "", name: str = "the texture") -> Image:
    """Bytes to a bitmap, by what they actually are rather than by what they claim.

    Hunyuan3D writes ``data:image/png;base64,`` in front of JPEG bytes, so the
    declaration is worse than useless here — it is actively wrong, and believing it sent
    a working texture down the "needs Pillow" path. It survives only in the message.
    """
    actual = sniff(data)
    if actual == "image/png":
        return decode_png(data)
    if actual == "image/jpeg":
        return decode_jpeg(data)
    return _decode_with_pillow(data, mime=actual or mime, name=name)


def read(path: Path) -> Image:
    return decode(path.read_bytes(), mime=SUFFIX_MIME.get(path.suffix.lower(), ""), name=path.name)


def _decode_with_pillow(data: bytes, *, mime: str, name: str) -> Image:
    try:
        from PIL import Image as PillowImage  # noqa: PLC0415 — optional by design
    except ImportError:
        raise ImageError(
            f"{name} is {mime or 'in a container this plugin does not recognise'}, and "
            "only PNG and baseline JPEG decode without help — add `pillow` to the "
            "plugin's requirements.txt and rebuild its environment on the Hunyuan3D "
            "Settings page, or re-export the mesh with a PNG or JPEG texture"
        ) from None

    import io  # noqa: PLC0415 — only on the path that already imported Pillow

    try:
        with PillowImage.open(io.BytesIO(data)) as opened:
            rgb = opened.convert("RGB")
            return Image(rgb.width, rgb.height, rgb.tobytes())
    except OSError as exc:
        raise ImageError(f"{name} could not be decoded: {exc}") from exc


# -------------------------------------------------------------------------------- png


def decode_png(data: bytes) -> Image:
    """A PNG, to RGB8.

    Every colour type and both byte depths, because "the texture is 16-bit" is not a
    reason to voxelise a model grey. Interlaced files are refused rather than
    mis-decoded: Adam7 is a second pass over seven sub-images and no exporter that
    reaches this plugin writes one.
    """
    if data[:8] != PNG_MAGIC:
        raise ImageError("Not a PNG")

    header: tuple[int, ...] | None = None
    palette = b""
    compressed = bytearray()
    at = 8
    while at + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[at : at + 8])
        body = data[at + 8 : at + 8 + length]
        if len(body) < length:
            raise ImageError("Truncated PNG chunk")
        at += 12 + length  # chunk header, body, CRC
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = body
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break

    if header is None:
        raise ImageError("PNG without an IHDR chunk")
    width, height, depth, colour, _compression, _filter, interlace = header
    if interlace:
        raise ImageError("Interlaced PNGs are not supported")
    if colour not in _CHANNELS:
        raise ImageError(f"Unknown PNG colour type {colour}")
    if depth not in (1, 2, 4, 8, 16):
        raise ImageError(f"Unknown PNG bit depth {depth}")
    if colour == 3 and not palette:
        raise ImageError("Indexed PNG without a palette")
    if not compressed:
        raise ImageError("PNG with no image data")

    channels = _CHANNELS[colour]
    try:
        raw = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ImageError(f"PNG image data could not be decompressed: {exc}") from exc

    scanlines = _unfilter(raw, width, height, channels, depth)
    return Image(width, height, _to_rgb(scanlines, width, height, channels, depth, colour, palette))


def _unfilter(raw: bytes, width: int, height: int, channels: int, depth: int) -> bytearray:
    """Undo the per-scanline filters, leaving the raw samples back to back.

    Written out byte by byte rather than reached for numpy: this is the one part of the
    decode that has to work in an environment with nothing installed, and a 512-square
    texture is under a megabyte.
    """
    bits = channels * depth
    stride = (width * bits + 7) // 8
    step = max(1, bits // 8)  # distance to the byte this one predicts from
    if len(raw) < height * (stride + 1):
        raise ImageError("PNG image data is shorter than its own dimensions")

    out = bytearray(height * stride)
    previous = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1 : start + 1 + stride])
        if kind == 1:  # Sub
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif kind == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:  # Average
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                upper_left = previous[i - step] if i >= step else 0
                up = previous[i]
                p = left + up - upper_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - upper_left)
                if pa <= pb and pa <= pc:
                    predictor = left
                elif pb <= pc:
                    predictor = up
                else:
                    predictor = upper_left
                line[i] = (line[i] + predictor) & 0xFF
        elif kind != 0:
            raise ImageError(f"Unknown PNG filter {kind} on row {row}")
        out[row * stride : (row + 1) * stride] = line
        previous = line
    return out


def _to_rgb(
    scanlines: bytearray,
    width: int,
    height: int,
    channels: int,
    depth: int,
    colour: int,
    palette: bytes,
) -> bytes:
    """Whatever the file stores, as three bytes per pixel."""
    bits = channels * depth
    stride = (width * bits + 7) // 8
    pixels = bytearray(width * height * 3)

    for row in range(height):
        line = scanlines[row * stride : (row + 1) * stride]
        for column in range(width):
            samples = _samples(line, column, channels, depth)
            out = (row * width + column) * 3
            if colour == 3:
                index = samples[0] * 3
                if index + 2 >= len(palette):
                    raise ImageError("Indexed PNG references a palette entry it does not have")
                pixels[out : out + 3] = palette[index : index + 3]
            elif colour in (0, 4):
                grey = _to_byte(samples[0], depth)
                pixels[out] = pixels[out + 1] = pixels[out + 2] = grey
            else:
                pixels[out] = _to_byte(samples[0], depth)
                pixels[out + 1] = _to_byte(samples[1], depth)
                pixels[out + 2] = _to_byte(samples[2], depth)
    return bytes(pixels)


def _samples(line: bytearray, column: int, channels: int, depth: int) -> list[int]:
    if depth == 8:
        at = column * channels
        return list(line[at : at + channels])
    if depth == 16:
        at = column * channels * 2
        return [(line[at + i * 2] << 8) | line[at + i * 2 + 1] for i in range(channels)]
    # Sub-byte depths are indexed or greyscale only, and so always one channel.
    per_byte = 8 // depth
    byte = line[column // per_byte]
    shift = 8 - depth * (column % per_byte + 1)
    return [(byte >> shift) & ((1 << depth) - 1)]


def _to_byte(sample: int, depth: int) -> int:
    if depth == 8:
        return sample
    if depth == 16:
        return sample >> 8
    # 1, 2 and 4 bits: spread the range over 0..255 rather than leaving a dark image.
    return round(sample * 255 / ((1 << depth) - 1))


# ------------------------------------------------------------------------------- jpeg

_SOF_BASELINE = (0xC0, 0xC1)
_SOF_UNSUPPORTED = {
    0xC2: "progressive",
    0xC3: "lossless",
    0xC5: "differential sequential",
    0xC6: "differential progressive",
    0xC7: "differential lossless",
    0xC9: "arithmetic sequential",
    0xCA: "arithmetic progressive",
    0xCB: "arithmetic lossless",
    0xCD: "differential arithmetic sequential",
    0xCE: "differential arithmetic progressive",
    0xCF: "differential arithmetic lossless",
}

SCALE = 8
"""How much smaller a decoded JPEG comes out. One pixel per 8x8 block, because only the
DC coefficient of each block is kept — see the module docstring for why that is enough."""


def decode_jpeg(data: bytes) -> Image:
    """A baseline JPEG, at 1/8 scale, from its DC coefficients alone.

    Every AC coefficient is still Huffman-decoded, because the bitstream cannot be
    stepped over without decoding it — but none is dequantised or transformed, which is
    where a full decode would spend its minute. The IDCT of a block with only a DC term
    is flat, and equal to ``DC / 8`` before the level shift, so there is nothing left to
    compute.
    """
    frame, scan, quant, huff, restart, payload = _read_jpeg(data)
    width, height, components = frame
    for component in components:
        if not quant.get(component.quant):
            raise ImageError(f"JPEG names quantization table {component.quant}, which it does not carry")
        if component.identifier not in scan:
            raise ImageError("JPEG scan does not cover every component of its frame")
        for table_class, table in zip((0, 1), scan[component.identifier]):
            if (table_class, table) not in huff:
                raise ImageError(f"JPEG names Huffman table {table_class}/{table}, which it does not carry")

    planes, strides = _decode_dc(frame, scan, quant, huff, restart, payload)
    return _assemble(width, height, components, planes, strides)


class _Component(NamedTuple):
    identifier: int
    h: int
    v: int
    quant: int


def _read_jpeg(data: bytes):
    """Everything ahead of the entropy-coded scan, plus the scan itself."""
    if data[:2] != JPEG_MAGIC:
        raise ImageError("Not a JPEG")

    quant: dict[int, list[int]] = {}
    huff: dict[tuple[int, int], list] = {}
    frame = None
    restart = 0
    at = 2
    while at + 4 <= len(data):
        if data[at] != 0xFF:
            at += 1
            continue
        marker = data[at + 1]
        if marker == 0xFF:
            at += 1  # fill byte
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
            at += 2
            continue
        if marker == 0xD9:
            break
        length = int.from_bytes(data[at + 2 : at + 4], "big")
        if length < 2:
            raise ImageError("Malformed JPEG segment length")
        body = data[at + 4 : at + 2 + length]
        if len(body) < length - 2:
            raise ImageError("Truncated JPEG segment")
        at += 2 + length

        if marker == 0xDB:
            _read_quant(body, quant)
        elif marker == 0xC4:
            _read_huffman(body, huff)
        elif marker == 0xDD:
            restart = int.from_bytes(body[:2], "big")
        elif marker in _SOF_UNSUPPORTED:
            raise ImageError(f"{_SOF_UNSUPPORTED[marker].capitalize()} JPEGs are not supported")
        elif marker in _SOF_BASELINE:
            frame = _read_frame(body)
        elif marker == 0xDA:
            if frame is None:
                raise ImageError("JPEG scan arrives before its frame header")
            return frame, _read_scan(body), quant, huff, restart, data[at:]
    raise ImageError("JPEG with no scan")


def _read_quant(body: bytes, quant: dict[int, list[int]]) -> None:
    at = 0
    while at < len(body):
        precision, identifier = body[at] >> 4, body[at] & 15
        at += 1
        size = 128 if precision else 64
        if at + size > len(body):
            raise ImageError("Truncated JPEG quantization table")
        if precision:
            quant[identifier] = [int.from_bytes(body[at + i * 2 : at + i * 2 + 2], "big") for i in range(64)]
        else:
            quant[identifier] = list(body[at : at + 64])
        at += size


def _read_huffman(body: bytes, huff: dict[tuple[int, int], list]) -> None:
    at = 0
    while at + 17 <= len(body):
        table_class, identifier = body[at] >> 4, body[at] & 15
        counts = list(body[at + 1 : at + 17])
        at += 17
        total = sum(counts)
        huff[(table_class, identifier)] = _lookup(counts, list(body[at : at + total]))
        at += total


def _lookup(counts: list[int], symbols: list[int]) -> list:
    """A 65536-entry table from the next 16 bits straight to ``(symbol, code length)``.

    Bit-at-a-time decoding costs up to sixteen iterations per symbol and a texture is a
    million symbols; one indexed lookup is the difference between three seconds and
    thirty. Filling it is a slice assignment per code, not a loop per entry.
    """
    table: list = [None] * 65536
    code = 0
    index = 0
    for length in range(1, 17):
        span = 1 << (16 - length)
        for _ in range(counts[length - 1]):
            if index >= len(symbols):
                raise ImageError("JPEG Huffman table declares more codes than it carries symbols")
            start = code << (16 - length)
            table[start : start + span] = [(symbols[index], length)] * span
            index += 1
            code += 1
        code <<= 1
    return table


def _read_frame(body: bytes):
    if len(body) < 6:
        raise ImageError("Truncated JPEG frame header")
    precision = body[0]
    height = int.from_bytes(body[1:3], "big")
    width = int.from_bytes(body[3:5], "big")
    count = body[5]
    if precision != 8:
        raise ImageError(f"{precision}-bit JPEGs are not supported")
    if count not in (1, 3):
        # Four means CMYK or YCCK, which also needs the Adobe APP14 transform flag to
        # read at all. No texture that reaches this plugin is one.
        raise ImageError(f"{count}-component JPEGs are not supported")
    if not width or not height:
        raise ImageError("JPEG declares a zero dimension")

    if len(body) < 6 + count * 3:
        raise ImageError("Truncated JPEG frame header")
    components = []
    for index in range(count):
        identifier, sampling, table = body[6 + index * 3 : 9 + index * 3]
        horizontal, vertical = (sampling >> 4) or 1, (sampling & 15) or 1
        if horizontal > 4 or vertical > 4:
            raise ImageError(f"JPEG sampling factor {horizontal}x{vertical} is out of range")
        components.append(_Component(identifier, horizontal, vertical, table))
    return width, height, components


def _read_scan(body: bytes) -> dict[int, tuple[int, int]]:
    if not body or len(body) < 1 + body[0] * 2:
        raise ImageError("Truncated JPEG scan header")
    tables: dict[int, tuple[int, int]] = {}
    for index in range(body[0]):
        identifier, spec = body[1 + index * 2], body[2 + index * 2]
        tables[identifier] = (spec >> 4, spec & 15)
    return tables


def _destuff(payload: bytes) -> tuple[bytes, list[int]]:
    """The scan with its byte stuffing removed, and where each restart marker landed.

    Split-and-rejoin rather than a loop over a megabyte: everything between markers is
    copied at C speed, and 0xFF is rare enough in entropy-coded data that the list stays
    short.
    """
    parts = payload.split(b"\xff")
    out = bytearray(parts[0])
    restarts: list[int] = []
    for part in parts[1:]:
        if not part:
            continue  # 0xFF 0xFF is a fill byte, not a marker
        marker = part[0]
        if marker == 0x00:
            out.append(0xFF)
            out += part[1:]
        elif 0xD0 <= marker <= 0xD7:
            restarts.append(len(out))
            out += part[1:]
        else:
            break  # a real marker: the scan ends here
    return bytes(out), restarts


class _Bits:
    """A big-endian bit reader over the destuffed scan."""

    __slots__ = ("data", "at", "buffer", "count")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0
        self.buffer = 0
        self.count = 0

    def _fill(self) -> None:
        # Past the end, feed zeros: a truncated texture should come back short of detail
        # rather than raise, since the colours already decoded are perfectly usable.
        data = self.data
        size = len(data)
        while self.count <= 24:
            byte = data[self.at] if self.at < size else 0
            self.at += 1
            self.buffer = (self.buffer << 8) | byte
            self.count += 8

    def symbol(self, table: list) -> int:
        if self.count < 16:
            self._fill()
        entry = table[(self.buffer >> (self.count - 16)) & 0xFFFF]
        if entry is None:
            raise ImageError("JPEG entropy data does not match its Huffman tables")
        symbol, length = entry
        self.count -= length
        self.buffer &= (1 << self.count) - 1
        return symbol

    def receive(self, length: int) -> int:
        if not length:
            return 0
        if self.count < length:
            self._fill()
        value = (self.buffer >> (self.count - length)) & ((1 << length) - 1)
        self.count -= length
        self.buffer &= (1 << self.count) - 1
        return value

    def restart(self, position: int) -> None:
        self.at = position
        self.buffer = 0
        self.count = 0


def _extend(value: int, length: int) -> int:
    """JPEG stores a coefficient as a magnitude category and its low bits."""
    return value - (1 << length) + 1 if value < (1 << (length - 1)) else value


def _decode_dc(frame, scan, quant, huff, restart: int, payload: bytes):
    """One sample per 8x8 block, per component, in MCU order."""
    _width, _height, components = frame
    h_max = max(component.h for component in components)
    v_max = max(component.v for component in components)
    mcus_x = -(-_width // (8 * h_max))
    mcus_y = -(-_height // (8 * v_max))

    strides = [mcus_x * component.h for component in components]
    planes = [bytearray(stride * mcus_y * component.v) for stride, component in zip(strides, components)]
    steps = [quant[component.quant][0] for component in components]
    dc_tables = [huff[(0, scan[component.identifier][0])] for component in components]
    ac_tables = [huff[(1, scan[component.identifier][1])] for component in components]

    data, restarts = _destuff(payload)
    bits = _Bits(data)
    predictions = [0] * len(components)
    segment = 0

    for mcu in range(mcus_x * mcus_y):
        if restart and mcu and mcu % restart == 0:
            if segment < len(restarts):
                bits.restart(restarts[segment])
                segment += 1
            predictions = [0] * len(components)
        mcu_x = mcu % mcus_x
        mcu_y = mcu // mcus_x
        for index, component in enumerate(components):
            dc_table = dc_tables[index]
            ac_table = ac_tables[index]
            step = steps[index]
            stride = strides[index]
            plane = planes[index]
            for by in range(component.v):
                row = (mcu_y * component.v + by) * stride
                for bx in range(component.h):
                    length = bits.symbol(dc_table)
                    if length:
                        predictions[index] += _extend(bits.receive(length), length)
                    k = 1
                    while k < 64:
                        rs = bits.symbol(ac_table)
                        size = rs & 15
                        if not size:
                            if rs != 0xF0:
                                break  # end of block
                            k += 16  # ZRL: sixteen zeros
                            continue
                        k += (rs >> 4) + 1
                        bits.receive(size)
                    # A DC-only block reconstructs flat, at DC/8, before the level shift.
                    value = (predictions[index] * step) // 8 + 128
                    plane[row + mcu_x * component.h + bx] = 0 if value < 0 else 255 if value > 255 else value
    return planes, strides


def _assemble(width: int, height: int, components, planes, strides) -> Image:
    """The component planes, chroma-upsampled by nearest and converted to RGB."""
    out_width = max(1, -(-width // SCALE))
    out_height = max(1, -(-height // SCALE))
    pixels = bytearray(out_width * out_height * 3)

    if len(components) == 1:
        plane, stride = planes[0], strides[0]
        rows = len(plane) // stride if stride else 0
        for y in range(out_height):
            row = min(y, rows - 1) * stride
            for x in range(out_width):
                grey = plane[row + min(x, stride - 1)]
                at = (y * out_width + x) * 3
                pixels[at] = pixels[at + 1] = pixels[at + 2] = grey
        return Image(out_width, out_height, bytes(pixels))

    h_max = max(component.h for component in components)
    v_max = max(component.v for component in components)
    luma, blue, red = planes
    luma_stride, blue_stride, red_stride = strides
    blue_x, blue_y = components[1].h, components[1].v
    red_x, red_y = components[2].h, components[2].v

    for y in range(out_height):
        luma_row = (y * components[0].v // v_max) * luma_stride
        blue_row = (y * blue_y // v_max) * blue_stride
        red_row = (y * red_y // v_max) * red_stride
        for x in range(out_width):
            brightness = luma[luma_row + x * components[0].h // h_max]
            cb = blue[blue_row + x * blue_x // h_max] - 128
            cr = red[red_row + x * red_x // h_max] - 128
            at = (y * out_width + x) * 3
            pixels[at] = _clamp(brightness + 1.402 * cr)
            pixels[at + 1] = _clamp(brightness - 0.344136 * cb - 0.714136 * cr)
            pixels[at + 2] = _clamp(brightness + 1.772 * cb)
    return Image(out_width, out_height, bytes(pixels))


def _clamp(value: float) -> int:
    return 0 if value < 0 else 255 if value > 255 else int(value)

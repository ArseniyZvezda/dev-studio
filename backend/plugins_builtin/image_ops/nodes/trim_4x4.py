"""Trim 4x4 — one picture in, its four quadrants out on four ports.

The cut is a straight 2x2 grid through the middle of the image, which is what a
four-up render sheet, an atlas page or a contact sheet of variations actually is.
There is nothing to tune: the only decision a splitter could offer is *where* to cut,
and anything other than the middle stops being this node and starts being a crop.

An odd dimension cannot halve evenly, so the extra row or column goes to the right and
bottom quadrants — stated because a caller reassembling the four parts has to know, and
because silently dropping it would lose a pixel line out of the middle of the picture.

Each quadrant is written as its own PNG named after the source bytes and the corner, so
the four never collide and re-running the node overwrites its own output rather than
piling up beside it. Like :mod:`jpeg_to_png` it reads whatever :mod:`imaging` can open,
which is PNG on the standard library alone and everything else once the plugin's
environment is built.
"""

from __future__ import annotations

import sys
import time

import _voxarium_studio as td
import imaging

QUADRANTS = ("top_left", "top_right", "bottom_left", "bottom_right")
"""The output ports, reading order. Duplicated in ``plugin.py``: a node script runs out
of process and cannot import it."""

TAGS = {
    "top_left": "tl",
    "top_right": "tr",
    "bottom_left": "bl",
    "bottom_right": "br",
}
"""What each quadrant is called in its filename — short, because the fingerprint that
follows it is what actually keeps the four files apart."""


def boxes(width: int, height: int) -> dict[str, tuple[int, int, int, int]]:
    """The four ``(left, top, width, height)`` rectangles of a 2x2 cut.

    The left column is ``width // 2`` and the right column is the rest, so the two always
    add back up to the source no matter how the halving rounded.
    """
    left = width // 2
    top = height // 2
    right = width - left
    bottom = height - top
    return {
        "top_left": (0, 0, left, top),
        "top_right": (left, 0, right, top),
        "bottom_left": (0, top, left, bottom),
        "bottom_right": (left, top, right, bottom),
    }


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    source = td.require_file(values.get("image"), "image")

    td.progress(percent=5, label="Reading", detail=source.name)
    started = time.monotonic()
    raster = imaging.read(source)
    if raster.width < 2 or raster.height < 2:
        td.fail(
            f"{source.name} is {raster.width}x{raster.height} — a quadrant cut needs at "
            "least 2 pixels on each side"
        )
    td.log(f"{source.name}: {raster.width}x{raster.height} -> four quadrants")

    assets = td.assets_dir(payload)
    rectangles = boxes(raster.width, raster.height)
    outputs: dict[str, str] = {}
    for index, quadrant in enumerate(QUADRANTS):
        left, top, width, height = rectangles[quadrant]
        td.progress(
            percent=10 + (index + 1) * 22,
            label="Writing",
            detail=f"{quadrant} ({width}x{height})",
            step=index + 1,
            steps=len(QUADRANTS),
        )
        target = assets / imaging.output_name(source, TAGS[quadrant])
        size = imaging.write_png(raster.crop(left, top, width, height), target)
        td.log(f"{quadrant}: {width}x{height} at ({left},{top}) -> {target.name} ({size} bytes)")
        outputs[quadrant] = str(target)

    td.log(f"cut in {time.monotonic() - started:.2f}s")
    td.emit(outputs)


if __name__ == "__main__":
    try:
        main()
    except imaging.ImageError as cause:
        td.fail(str(cause))
    except MemoryError:
        td.fail("Out of memory — the image is too large to cut in one piece", code=1)
    except KeyboardInterrupt:  # the runner cancels a node by killing the process
        sys.exit(130)

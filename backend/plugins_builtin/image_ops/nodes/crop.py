"""Crop — the alpha channel's bounding box, framed at whatever resolution is asked for.

Two things in one node, because they are one decision. The bounding box is measured from
the alpha channel: the smallest rectangle holding every pixel the image actually draws,
which is what turns a keyed cutout floating in a sea of transparency back into a picture of
its subject. The output resolution then says how much room that subject gets, and the box
is centred in it — padded with transparency when the frame is the larger, and shrunk to fit
when it is the smaller.

**A frame with room to spare never resamples.** The subject is placed pixel for pixel, so
the ordinary trim and the ordinary pad cost the picture nothing. Only a frame the box will
not fit in reaches :meth:`imaging.Raster.resample_median`, and it shrinks by one factor on
both axes so the subject keeps its proportions, the slack becoming transparent margin. The
filter is a **median**, not an average: an average of two colours is a third colour that was
never in the picture, and on a keyed sprite that third colour is a rim of half-background.

Pairs with Remove Background, which is where the alpha usually comes from: key the backdrop
out, then cut off the empty margin the backdrop was filling. A picture carrying no alpha at
all is opaque everywhere, so its box is the whole picture and the node only ever frames it.
"""

from __future__ import annotations

import sys
import time

import _voxarium_studio as td
import imaging

DEFAULT_THRESHOLD = 0.0
"""Anything not fully transparent counts as subject. Duplicated in ``plugin.py``: a node
script runs out of process and cannot import it."""


def cutoff_from(value, default: float = DEFAULT_THRESHOLD) -> int:
    """The alpha value at or below which a pixel is empty, from a 0-100 percentage.

    A percentage rather than a raw 0-255 alpha, because that is the vocabulary the rest of
    this plugin already uses for a strength, and because every useful setting is at the
    bottom of the range: 0 takes every drawn pixel, and a few percent is enough to ignore
    the faint halo an antialiased cut leaves behind.
    """
    try:
        percent = float(value)
    except (TypeError, ValueError):
        percent = default
    if percent != percent:  # NaN, which neither comparison below would catch
        percent = default
    return round(min(100.0, max(0.0, percent)) * 255 / 100)


def dimension(value, label: str) -> int:
    """One side of the output frame: a whole number of pixels, or 0 for "as measured".

    0 rather than an empty field, because the port is a number — the config panel draws a
    spinner for it and an upstream node can drive it. It reads as "no opinion", which is
    exactly what the plain trim is.
    """
    if value is None or value == "":
        return 0
    try:
        pixels = int(round(float(value)))
    except (TypeError, ValueError):
        td.fail(f"{label} must be a whole number of pixels, got {value!r}")
        raise  # unreachable; `fail` exits, and this keeps the type checker honest
    if pixels < 0:
        td.fail(f"{label} cannot be negative, got {pixels}")
    return pixels


def frame_for(
    bounds: tuple[int, int, int, int], width: int, height: int
) -> tuple[int, int, int, int]:
    """The *width* x *height* frame centred on *bounds*, in whatever space bounds is given.

    Used twice and in two spaces, which is why it takes the box rather than reading one: to
    place the finished content inside the output (the box at the origin), and to say which
    region of the *source* the output corresponds to, for the node's overlay.

    An odd difference cannot be split evenly. Flooring puts the frame's centre half a pixel
    right of and below the box's, whichever of the two is larger: a wider frame pads one
    more column on the right than on the left, a narrower one takes one more column off the
    left than off the right. It is one rule, worth stating because somebody comparing two
    runs at neighbouring resolutions will otherwise watch the subject twitch and wonder
    which way it went.
    """
    left, top, box_width, box_height = bounds
    out_width = width or box_width
    out_height = height or box_height
    return (
        left - (out_width - box_width) // 2,
        top - (out_height - box_height) // 2,
        out_width,
        out_height,
    )


def fit(bounds: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, float]:
    """The output size, and the factor the bounding box must shrink by to fit inside it.

    The factor is 1.0 whenever the box already fits, and that is the design: **shrinking is
    what a frame smaller than the subject asks for, and nothing else.** A frame with room to
    spare still pads, pixel for pixel, because resampling a picture that did not need
    resampling would cost it detail for no reason at all.

    One factor for both axes, so the subject keeps its proportions and the slack on the
    other axis becomes transparent margin — "fit inside and centre", never "stretch to
    fill". Stretching would be the one framing that changes what the subject *is*.
    """
    _, _, box_width, box_height = bounds
    out_width = width or box_width
    out_height = height or box_height
    return out_width, out_height, min(1.0, out_width / box_width, out_height / box_height)


def shrunk_to(bounds: tuple[int, int, int, int], scale: float) -> tuple[int, int]:
    """What the bounding box measures once *scale* is applied — at least one pixel each way.

    The floor matters: a frame small enough to round one side of a long thin subject to zero
    is a strange thing to ask for, and a zero-sized raster is a crash rather than an answer.
    """
    _, _, box_width, box_height = bounds
    if scale >= 1.0:
        return box_width, box_height
    return max(1, round(box_width * scale)), max(1, round(box_height * scale))


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)

    source = td.require_file(values.get("image"), "image")
    cutoff = cutoff_from(values.get("alpha_threshold"))
    width = dimension(values.get("width"), "Width")
    height = dimension(values.get("height"), "Height")

    td.progress(percent=5, label="Reading", detail=source.name)
    started = time.monotonic()
    raster = imaging.read(source)
    td.log(f"{source.name}: {raster.width}x{raster.height}, {raster.count} pixels")

    td.progress(percent=40, label="Measuring", detail=f"alpha above {cutoff}")
    bounds = raster.alpha_bounds(cutoff)
    if bounds is None:
        # Not the same case as "nothing matched" in Remove Background, which still has a
        # picture to hand on. There is no rectangle here, so there is nothing to write.
        td.fail(
            f"{source.name} has no pixel above the alpha threshold, so there is no bounding "
            "box to crop to. Lower the threshold, or key a background out first."
        )
        return
    box_left, box_top, box_width, box_height = bounds
    covered = 100.0 * box_width * box_height / raster.count if raster.count else 0.0
    td.log(
        f"bounding box: {box_width}x{box_height} at ({box_left},{box_top}) "
        f"— {covered:.1f}% of the image"
    )
    if (box_width, box_height) == (raster.width, raster.height):
        # Worth saying out loud: an opaque source — a JPEG, a flattened PNG — reaches every
        # edge, and a node that appears to have done nothing is otherwise indistinguishable
        # from a broken one.
        td.log("the subject reaches every edge, so there is no margin to trim")

    out_width, out_height, scale = fit(bounds, width, height)

    # The box itself, and only ever the box: whatever sits outside it in the source was
    # below the threshold, so keeping it would be keeping exactly the halo the threshold was
    # raised to be rid of.
    content = raster.window(*bounds)
    if scale < 1.0:
        shrunk_width, shrunk_height = shrunk_to(bounds, scale)
        td.progress(
            percent=60,
            label="Shrinking",
            detail=f"{box_width}x{box_height} -> {shrunk_width}x{shrunk_height}",
        )
        content = content.resample_median(shrunk_width, shrunk_height)
        td.log(
            f"the frame is {out_width}x{out_height} and the box is {box_width}x{box_height}, "
            f"so the box is shrunk by {scale:.4g} to {shrunk_width}x{shrunk_height} — "
            "median filtered, so no colour is invented"
        )

    placement = frame_for((0, 0, content.width, content.height), out_width, out_height)

    td.progress(percent=85, label="Writing", detail=f"{out_width}x{out_height}")
    target = td.assets_dir(payload) / imaging.output_name(
        source, "crop", cutoff, out_width, out_height
    )
    size = imaging.write_png(content.window(*placement), target)

    # Which region of the *source* the output frame corresponds to. It is the output size
    # divided back through the scale, so it degenerates to the frame itself when nothing was
    # shrunk — and either way it is what the node's overlay draws over the source.
    span = frame_for(
        bounds,
        max(box_width, round(out_width / scale)),
        max(box_height, round(out_height / scale)),
    )

    td.log(
        f"framed {out_width}x{out_height}, the box centred in it, "
        f"in {time.monotonic() - started:.2f}s"
    )
    td.log(f"wrote {target} ({size} bytes)")

    # `source`, `bounds`, `frame`, `scale` and `source_size` are not declared ports,
    # deliberately: they are what the node's own canvas component draws the box and the
    # frame from, and none of them is something a downstream node would ever be wired to.
    td.emit(
        {
            "image": str(target),
            "source": str(source),
            "bounds": [box_left, box_top, box_width, box_height],
            "frame": list(span),
            "size": [out_width, out_height],
            "scale": scale,
            "source_size": [raster.width, raster.height],
        }
    )


if __name__ == "__main__":
    try:
        main()
    except imaging.ImageError as cause:
        td.fail(str(cause))
    except MemoryError:
        td.fail("Out of memory — the image is too large to frame in one piece", code=1)
    except KeyboardInterrupt:  # the runner cancels a node by killing the process
        sys.exit(130)

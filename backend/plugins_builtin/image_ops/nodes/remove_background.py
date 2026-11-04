"""Remove Background — one colour keyed out, a PNG with an alpha channel back.

The node script itself is only the wiring: read the protocol payload, turn six parameters
into a :class:`keying.KeyOptions`, and put the result somewhere the rest of the pipeline
can find it. The picture work lives in :mod:`imaging` (decode/encode) and :mod:`keying`
(the wand), which is also what makes both testable without a subprocess or a venv.
"""

from __future__ import annotations

import sys
import time

import _voxarium_studio as td
import imaging
import keying


def options_from(values: dict) -> keying.KeyOptions:
    try:
        key_color = keying.parse_color(values.get("key_color", "#ffffff"))
    except ValueError as cause:
        td.fail(str(cause))
        raise  # unreachable; `fail` exits, and this keeps the type checker honest

    region = str(values.get("region") or "edges").strip().lower()
    if region not in keying.REGIONS:
        td.fail(f"Unknown region {region!r}; expected one of {list(keying.REGIONS)}")

    antialias = values.get("antialias")
    return keying.KeyOptions(
        key_color=key_color,
        tolerance=keying.clamp_percent(values.get("tolerance"), 12.0),
        region=region,
        edge_sharpness=keying.clamp_percent(values.get("edge_sharpness"), 80.0),
        # Unset means the default, and the default is on — "antialias after the cut" is
        # what the node promises, not something to opt into.
        antialias=True if antialias is None else bool(antialias),
    )


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)

    source = td.require_file(values.get("image"), "image")
    options = options_from(values)
    hex_color = "#%02x%02x%02x" % options.key_color

    td.progress(percent=5, label="Reading", detail=source.name)
    started = time.monotonic()
    try:
        raster = imaging.read(source)
    except imaging.ImageError as cause:
        td.fail(str(cause))
        return
    td.log(f"{source.name}: {raster.width}x{raster.height}, {raster.count} pixels")

    td.progress(percent=35, label="Keying", detail=f"{hex_color} ±{options.tolerance:g}%")
    result = keying.key_out(raster.pixels, raster.width, raster.height, options)

    if options.antialias:
        td.progress(percent=75, label="Antialiasing")

    td.progress(percent=85, label="Writing")
    target = td.assets_dir(payload) / imaging.output_name(
        source,
        "nobg",
        options.key_color,
        round(options.tolerance, 4),
        options.region,
        round(options.edge_sharpness, 4),
        options.antialias,
    )
    size = imaging.write_png(raster.with_alpha(result.alpha), target)

    share = 100.0 * result.removed / raster.count if raster.count else 0.0
    td.log(
        f"keyed {hex_color} out of {share:.1f}% of the image "
        f"({result.removed} pixels transparent, {result.softened} on the edge) "
        f"in {time.monotonic() - started:.2f}s"
    )
    if not result.removed:
        # Not a failure: a picture whose corners are already the subject is a real thing to
        # hand this node. Saying so beats an unchanged-looking image and no explanation.
        td.log(
            "nothing matched — check the key colour, raise the tolerance, or switch the "
            "region to 'everywhere' if the background is not connected to the border"
        )
    td.log(f"wrote {target} ({size} bytes)")

    # `source` is not a declared port and deliberately so: it is what the node's own canvas
    # component samples the eyedropper from, and it must not become a pin nobody wires.
    td.emit(
        {
            "image": str(target),
            "source": str(source),
            "removed_pixels": result.removed,
            "edge_pixels": result.softened,
        }
    )


if __name__ == "__main__":
    try:
        main()
    except imaging.ImageError as cause:
        td.fail(str(cause))
    except MemoryError:
        td.fail("Out of memory — the image is too large to key in one piece", code=1)
    except KeyboardInterrupt:  # the runner cancels a node by killing the process
        sys.exit(130)

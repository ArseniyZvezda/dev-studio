"""JPEG to PNG — the same picture, in the container the rest of a pipeline can read.

Thin on purpose, and thinner than :mod:`remove_background`: :mod:`imaging` already turns a
file into RGBA8 and writes a PNG back, so all that is left here is the protocol wiring.

The one thing worth knowing before running it is that **this node needs the plugin's
environment**, where "Remove Background" does not. PNG is what ``imaging`` reads on `zlib`
alone; JPEG is precisely the container it refuses to decode itself, because two IDCTs need
not agree in the last bit and the §8.2 cache key cannot see which decoder ran. Pillow is
therefore not an optimisation here, it is the decoder — and ``imaging.read`` already says
so, naming the button that installs it.

Nothing else is decided by the suffix. Whatever Pillow opens is converted, so a WebP or a
BMP arriving on the port comes out as a PNG rather than as an error about its name, and a
PNG that is already a PNG is re-encoded rather than refused — a converter that broke a
chain the moment its upstream changed format would be a worse node than a lenient one.
"""

from __future__ import annotations

import sys
import time

import _voxarium_studio as td
import imaging


def main() -> None:
    payload = td.read_payload()
    values = td.resolved(payload)
    source = td.require_file(values.get("image"), "image")

    td.progress(percent=10, label="Reading", detail=source.name)
    started = time.monotonic()
    raster = imaging.read(source)
    td.log(f"{source.name}: {raster.width}x{raster.height}, {raster.count} pixels")

    td.progress(percent=70, label="Writing")
    target = td.assets_dir(payload) / imaging.output_name(source)
    size = imaging.write_png(raster, target)

    # RGBA even when the source had no alpha, which a JPEG never does. The opaque channel
    # is a flat plane, so `Up` filtering turns it into a run of zeros and deflate charges
    # almost nothing for it — and the alternative is a second encoder path for a saving
    # nobody would measure.
    td.log(f"wrote {target} ({size} bytes) in {time.monotonic() - started:.2f}s")
    td.emit({"image": str(target)})


if __name__ == "__main__":
    try:
        main()
    except imaging.ImageError as cause:
        td.fail(str(cause))
    except MemoryError:
        td.fail("Out of memory — the image is too large to convert in one piece", code=1)
    except KeyboardInterrupt:  # the runner cancels a node by killing the process
        sys.exit(130)

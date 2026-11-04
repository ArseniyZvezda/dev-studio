"""The image_ops plugin: the schema the canvas builds from, and the wand behind it.

The node-side modules are imported the way a node script imports them — ``nodes/`` on
``sys.path``, no ``voxarium_studio`` package in sight — because that is the only environment they
are ever run in. They are pure standard library on purpose, which is what lets the real
keying be checked here rather than only through a subprocess that needs a provisioned venv.

Pictures are built as raw RGBA rather than through Pillow for the same reason: the suite
must exercise the path a fresh install actually takes.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from voxarium_studio.plugins import loader as plugin_loader
from voxarium_studio.plugins.registry import Registry

BUILTIN = plugin_loader.builtin_plugins_dir()
assert BUILTIN is not None, "the built-in plugin directory must be discoverable"
NODES = BUILTIN / "image_ops" / "nodes"

NODE_TYPE = "image_ops.remove_background"
CONVERT_TYPE = "image_ops.jpeg_to_png"
TRIM_TYPE = "image_ops.trim_4x4"
CROP_TYPE = "image_ops.crop"
QUADRANTS = ("top_left", "top_right", "bottom_left", "bottom_right")
NODE_MODULES = (
    "imaging",
    "keying",
    "remove_background",
    "jpeg_to_png",
    "trim_4x4",
    "crop",
    "_voxarium_studio",
)

WHITE = (255, 255, 255)


@pytest.fixture
def nodes(monkeypatch: pytest.MonkeyPatch):
    """Import the plugin's modules as a node script does, leaving `sys.modules` as it was."""
    monkeypatch.syspath_prepend(str(NODES))
    for name in NODE_MODULES:
        sys.modules.pop(name, None)
    import imaging
    import keying

    yield imaging, keying
    for name in NODE_MODULES:
        sys.modules.pop(name, None)


def make_pixels(width: int, height: int, colour_at) -> bytearray:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes(colour_at(x, y))
    return pixels


def disc_on_white(size: int = 32, radius: float = 10.0) -> bytearray:
    """A solid disc on white, with one white pixel *inside* it — the enclosed-colour case."""

    def colour(x: int, y: int) -> tuple[int, int, int, int]:
        if (x, y) == (size // 2, size // 2):
            return (255, 255, 255, 255)
        inside = ((x - size / 2) ** 2 + (y - size / 2) ** 2) ** 0.5 <= radius
        return (40, 90, 200, 255) if inside else (255, 255, 255, 255)

    return make_pixels(size, size, colour)


def alpha_at(alpha: bytes, width: int, x: int, y: int) -> int:
    return alpha[y * width + x]


# ------------------------------------------------------------------ the schema


def test_the_node_is_registered_with_an_image_in_and_a_png_out(registry: Registry) -> None:
    schema = registry.node_type(NODE_TYPE)
    assert schema is not None
    assert schema.plugin_id == "image_ops"
    assert schema.name == "Remove Background"
    assert schema.category == "Image"
    assert schema.input("image").type == "file"
    assert schema.input("image").required
    # "any format" is the promise; the accept list is the file picker's hint at it.
    for suffix in (".png", ".jpg", ".webp", ".bmp", ".tga"):
        assert suffix in schema.input("image").accept
    assert [(port.id, port.type) for port in schema.outputs] == [("image", "file")]
    assert schema.outputs[0].accept == [".png"]


def test_the_key_colour_is_a_colour_port_defaulting_to_white(registry: Registry) -> None:
    """The port type is what puts a swatch and an eyedropper in the config panel."""
    port = registry.node_type(NODE_TYPE).input("key_color")
    assert port.type == "color"
    assert port.default == "#FFFFFF"
    assert port.connectable is False


def test_the_tuning_ports_carry_the_defaults_the_node_promises(registry: Registry) -> None:
    """Antialiasing is on unless it is turned off, and the cut is crisp but not stepped."""
    schema = registry.node_type(NODE_TYPE)
    assert schema.input("tolerance").default == 12
    assert (schema.input("tolerance").minimum, schema.input("tolerance").maximum) == (0, 100)
    assert schema.input("region").default == "edges"
    assert schema.input("region").options == ["edges", "everywhere"]
    assert schema.input("edge_sharpness").default == 80
    assert schema.input("antialias").default is True


def test_the_node_runs_out_of_process_and_caches(registry: Registry) -> None:
    """Keying a 4096-square texture is not event-loop work, and it is pure of side effects."""
    execution = registry.node_type(NODE_TYPE).execution
    assert execution.kind == "python_script"
    assert execution.entry == "nodes/remove_background.py"
    assert execution.env == "plugin_venv"
    assert execution.resource_class == "cpu"
    assert execution.cacheable is True


def test_the_plugin_ships_the_canvas_component(registry: Registry) -> None:
    """Without it there is no eyedropper, and the key colour can only be typed."""
    assert registry.node_type(NODE_TYPE).ui_component == "frontend/RemoveBackgroundNode.js"


def test_the_converter_is_registered_with_a_jpeg_in_and_a_png_out(registry: Registry) -> None:
    schema = registry.node_type(CONVERT_TYPE)
    assert schema is not None
    assert schema.plugin_id == "image_ops"
    assert schema.name == "JPEG to PNG"
    assert schema.category == "Image"
    assert schema.input("image").type == "file"
    assert schema.input("image").required
    for suffix in (".jpg", ".jpeg"):
        assert suffix in schema.input("image").accept
    assert [(port.id, port.type) for port in schema.outputs] == [("image", "file")]
    assert schema.outputs[0].accept == [".png"]


def test_the_converter_has_nothing_to_tune_and_no_component_of_its_own(
    registry: Registry,
) -> None:
    """One port each way and the generic renderer — a small node, not a panel.

    The absent `ui_component` is the assertion that matters: there is no setting to aim and
    no result worth drawing, so the node has no reason to carry a preview around a chain.
    """
    schema = registry.node_type(CONVERT_TYPE)
    assert schema.ui_component is None
    assert [port.id for port in schema.inputs] == ["image"]


def test_the_converter_runs_out_of_process_in_the_environment_that_has_pillow(
    registry: Registry,
) -> None:
    """JPEG is the one container `imaging` will not decode itself, so the venv is the point."""
    execution = registry.node_type(CONVERT_TYPE).execution
    assert execution.kind == "python_script"
    assert execution.entry == "nodes/jpeg_to_png.py"
    assert execution.env == "plugin_venv"
    assert execution.resource_class == "cpu"
    assert execution.cacheable is True


def test_the_trimmer_is_registered_with_one_image_in_and_four_out(registry: Registry) -> None:
    """Four ports rather than one folder: the corners go to four different places."""
    schema = registry.node_type(TRIM_TYPE)
    assert schema is not None
    assert schema.plugin_id == "image_ops"
    assert schema.name == "Trim 4x4"
    assert schema.category == "Image"
    assert [port.id for port in schema.inputs] == ["image"]
    assert schema.input("image").type == "file"
    assert schema.input("image").required
    assert [(port.id, port.type) for port in schema.outputs] == [
        (quadrant, "file") for quadrant in QUADRANTS
    ]
    assert all(port.accept == [".png"] for port in schema.outputs)


def test_the_trimmer_has_nothing_to_tune_and_no_component_of_its_own(
    registry: Registry,
) -> None:
    """Where to cut is the only setting a halving could offer, and it is the middle."""
    schema = registry.node_type(TRIM_TYPE)
    assert schema.ui_component is None
    assert schema.execution.kind == "python_script"
    assert schema.execution.entry == "nodes/trim_4x4.py"
    assert schema.execution.env == "plugin_venv"
    assert schema.execution.cacheable is True


def test_the_cropper_is_registered_with_an_image_in_and_a_png_out(registry: Registry) -> None:
    schema = registry.node_type(CROP_TYPE)
    assert schema is not None
    assert schema.plugin_id == "image_ops"
    assert schema.name == "Crop"
    assert schema.category == "Image"
    assert schema.input("image").type == "file"
    assert schema.input("image").required
    for suffix in (".png", ".webp", ".tga"):
        assert suffix in schema.input("image").accept
    assert [(port.id, port.type) for port in schema.outputs] == [("image", "file")]
    assert schema.outputs[0].accept == [".png"]


def test_the_output_size_defaults_to_the_bounding_box_itself(registry: Registry) -> None:
    """0 is "no opinion", which is the plain trim — the node's whole job without a frame."""
    schema = registry.node_type(CROP_TYPE)
    assert [port.id for port in schema.inputs] == ["image", "alpha_threshold", "width", "height"]
    for side in ("width", "height"):
        assert schema.input(side).type == "integer"
        assert schema.input(side).default == 0
        assert schema.input(side).minimum == 0
        assert schema.input(side).connectable is False
    assert schema.input("alpha_threshold").default == 0
    assert (
        schema.input("alpha_threshold").minimum,
        schema.input("alpha_threshold").maximum,
    ) == (0, 100)


def test_the_cropper_runs_out_of_process_and_draws_its_own_node(registry: Registry) -> None:
    """The component is where the measured box and the frame are visible at all."""
    schema = registry.node_type(CROP_TYPE)
    assert schema.ui_component == "frontend/CropNode.js"
    execution = schema.execution
    assert execution.kind == "python_script"
    assert execution.entry == "nodes/crop.py"
    assert execution.env == "plugin_venv"
    assert execution.resource_class == "cpu"
    assert execution.cacheable is True


# ------------------------------------------------------------------ colours


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#ffffff", (255, 255, 255)),
        ("#FFF", (255, 255, 255)),
        ("00ff80", (0, 255, 128)),
        ("#12345678", (0x12, 0x34, 0x56)),  # the alpha is not a colour to key against
        ("rgb(10, 20, 30)", (10, 20, 30)),
        ([1, 2, 3], (1, 2, 3)),
        ((300, -5, 7), (255, 0, 7)),
    ],
)
def test_a_colour_is_read_from_every_spelling_the_ui_can_produce(nodes, raw, expected) -> None:
    _, keying = nodes
    assert keying.parse_color(raw) == expected


@pytest.mark.parametrize("raw", ["", "white", "#12345", "#gggggg", None])
def test_an_unreadable_colour_is_refused_rather_than_defaulted(nodes, raw) -> None:
    """Falling back to white would key the picture against something nobody chose."""
    _, keying = nodes
    with pytest.raises(ValueError):
        keying.parse_color(raw)


# ------------------------------------------------------------------ the wand


def test_distance_is_the_largest_difference_on_any_one_channel(nodes) -> None:
    """Chebyshev, like a magic wand: one channel being far off is enough to differ."""
    _, keying = nodes
    pixels = bytearray([255, 255, 255, 255, 250, 255, 255, 255, 255, 0, 255, 255])
    assert list(keying.difference(pixels, WHITE)) == [0, 5, 255]


def test_full_sharpness_is_a_hard_threshold_at_the_tolerance(nodes) -> None:
    _, keying = nodes
    lut = keying.strength_lut(tolerance=10, sharpness=100)
    # 10% of 255 is 25.5, so 25 is inside the band and 26 is outside it.
    assert lut[0] == 255 and lut[25] == 255
    assert lut[26] == 0 and lut[255] == 0
    # Nothing in between: a hard cut is the one that antialiasing then softens.
    assert set(lut) == {0, 255}


def test_lower_sharpness_fades_the_alpha_across_the_band(nodes) -> None:
    _, keying = nodes
    lut = keying.strength_lut(tolerance=100, sharpness=0)
    assert lut[0] == 255 and lut[255] == 0
    partial = [value for value in lut if 0 < value < 255]
    assert len(partial) > 200
    assert partial == sorted(partial, reverse=True)


def test_zero_tolerance_keys_only_the_colour_itself(nodes) -> None:
    _, keying = nodes
    lut = keying.strength_lut(tolerance=0, sharpness=80)
    assert lut[0] == 255
    assert set(lut[1:]) == {0}


def test_the_edges_region_keeps_matching_colour_the_subject_encloses(nodes) -> None:
    """The white glint inside a disc is the whole reason the flood fill exists."""
    _, keying = nodes
    size = 32
    pixels = disc_on_white(size)
    result = keying.key_out(
        pixels, size, size, keying.KeyOptions(tolerance=10, antialias=False, region="edges")
    )
    assert alpha_at(result.alpha, size, 0, 0) == 0
    assert alpha_at(result.alpha, size, size // 2, size // 2) == 255
    assert alpha_at(result.alpha, size, size // 2, size // 2 - 4) == 255


def test_the_everywhere_region_takes_that_same_colour_out(nodes) -> None:
    _, keying = nodes
    size = 32
    pixels = disc_on_white(size)
    result = keying.key_out(
        pixels, size, size, keying.KeyOptions(tolerance=10, antialias=False, region="everywhere")
    )
    assert alpha_at(result.alpha, size, 0, 0) == 0
    assert alpha_at(result.alpha, size, size // 2, size // 2) == 0


def test_an_unknown_region_is_refused(nodes) -> None:
    _, keying = nodes
    with pytest.raises(ValueError):
        keying.key_out(bytearray(4), 1, 1, keying.KeyOptions(region="corners"))


def test_a_hard_cut_leaves_no_partial_alpha_until_antialiasing(nodes) -> None:
    _, keying = nodes
    size = 32
    pixels = disc_on_white(size)
    options = keying.KeyOptions(tolerance=10, edge_sharpness=100, antialias=False)
    hard = keying.key_out(pixels, size, size, options)
    assert hard.softened == 0
    assert set(hard.alpha) == {0, 255}

    smooth = keying.key_out(
        pixels, size, size, keying.KeyOptions(tolerance=10, edge_sharpness=100, antialias=True)
    )
    assert smooth.softened > 0
    # The subject's middle and the far corner are untouched: a blur of a flat region is
    # that region, which is why nothing has to detect where the edge is.
    assert alpha_at(smooth.alpha, size, size // 2, size // 2) == 255
    assert alpha_at(smooth.alpha, size, 0, 0) == 0


def test_a_pixel_the_source_had_already_made_transparent_stays_transparent(nodes) -> None:
    """The source's own alpha is intersected, so a keyed cutout can be keyed again."""
    _, keying = nodes
    pixels = bytearray([40, 90, 200, 0, 40, 90, 200, 255])
    result = keying.key_out(pixels, 2, 1, keying.KeyOptions(tolerance=10, antialias=False))
    assert list(result.alpha) == [0, 255]


def test_nothing_matching_is_a_result_and_not_an_error(nodes) -> None:
    _, keying = nodes
    size = 8
    pixels = make_pixels(size, size, lambda x, y: (10, 20, 30, 255))
    result = keying.key_out(pixels, size, size, keying.KeyOptions(tolerance=5, antialias=False))
    assert result.removed == 0
    assert set(result.alpha) == {255}


def test_the_flood_fill_reaches_around_a_barrier(nodes) -> None:
    """A span fill that only walked rows would leave the far side of a wall behind."""
    _, keying = nodes
    width, height = 7, 5
    # A vertical wall down the middle, open along the bottom row.
    def colour(x: int, y: int) -> tuple[int, int, int, int]:
        wall = x == 3 and y < height - 1
        return (0, 0, 0, 255) if wall else (255, 255, 255, 255)

    pixels = make_pixels(width, height, colour)
    result = keying.key_out(
        pixels, width, height, keying.KeyOptions(tolerance=5, antialias=False, region="edges")
    )
    assert alpha_at(result.alpha, width, 0, 0) == 0
    assert alpha_at(result.alpha, width, 6, 0) == 0
    assert alpha_at(result.alpha, width, 3, 0) == 255  # the wall itself survives


def test_an_enclosed_pocket_is_never_reached(nodes) -> None:
    _, keying = nodes
    width = height = 5
    ring = {(x, y) for x in range(1, 4) for y in range(1, 4)} - {(2, 2)}
    pixels = make_pixels(
        width, height, lambda x, y: (0, 0, 0, 255) if (x, y) in ring else (255, 255, 255, 255)
    )
    result = keying.key_out(
        pixels, width, height, keying.KeyOptions(tolerance=5, antialias=False, region="edges")
    )
    assert alpha_at(result.alpha, width, 2, 2) == 255
    assert alpha_at(result.alpha, width, 0, 0) == 0


# ------------------------------------------------------------------ image IO


def png_chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def build_png(width: int, height: int, depth: int, colour: int, rows: bytes, extra=()) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    chunks = [b"\x89PNG\r\n\x1a\n", png_chunk(b"IHDR", header)]
    chunks += [png_chunk(kind, body) for kind, body in extra]
    chunks += [png_chunk(b"IDAT", zlib.compress(rows)), png_chunk(b"IEND", b"")]
    return b"".join(chunks)


def test_a_written_png_reads_back_pixel_for_pixel(nodes) -> None:
    imaging, _ = nodes
    size = 16
    raster = imaging.Raster(size, size, make_pixels(size, size, lambda x, y: (x * 8, y * 8, 7, 200)))
    assert imaging.decode_png(imaging.encode_png(raster)).pixels == raster.pixels


@pytest.mark.parametrize(
    ("colour", "rows", "expected"),
    [
        # Truecolour: alpha is invented, because the format has none.
        (2, b"\x00\x0a\x14\x1e\x28\x32\x3c", [(10, 20, 30, 255), (40, 50, 60, 255)]),
        # Greyscale: one sample painted across three channels.
        (0, b"\x00\x40\x80", [(64, 64, 64, 255), (128, 128, 128, 255)]),
        # Greyscale + alpha.
        (4, b"\x00\x40\x10\x80\x20", [(64, 64, 64, 16), (128, 128, 128, 32)]),
        # Truecolour + alpha, which needs no expansion at all.
        (6, b"\x00\x01\x02\x03\x04\x05\x06\x07\x08", [(1, 2, 3, 4), (5, 6, 7, 8)]),
    ],
)
def test_every_png_colour_type_expands_to_rgba(nodes, colour, rows, expected) -> None:
    """A plugin that could only read its own output would be a plugin for one format."""
    imaging, _ = nodes
    raster = imaging.decode_png(build_png(2, 1, 8, colour, rows))
    assert [tuple(raster.pixels[i * 4 : i * 4 + 4]) for i in range(2)] == expected


def test_an_indexed_png_takes_its_alpha_from_the_palette(nodes) -> None:
    imaging, _ = nodes
    palette = bytes((255, 0, 0, 0, 255, 0))
    data = build_png(
        2, 1, 8, 3, b"\x00\x00\x01", extra=[(b"PLTE", palette), (b"tRNS", bytes((0, 128)))]
    )
    raster = imaging.decode_png(data)
    assert tuple(raster.pixels[:4]) == (255, 0, 0, 0)
    assert tuple(raster.pixels[4:]) == (0, 255, 0, 128)


def test_sixteen_bit_samples_come_down_to_eight(nodes) -> None:
    """The high byte, which is what `convert("RGBA")` keeps too — so both decoders agree."""
    imaging, _ = nodes
    rows = b"\x00" + struct.pack(">6H", 0x1234, 0x5678, 0x9ABC, 0xDEF0, 0x1122, 0x3344)
    raster = imaging.decode_png(build_png(2, 1, 16, 2, rows))
    assert tuple(raster.pixels[:4]) == (0x12, 0x56, 0x9A, 255)
    assert tuple(raster.pixels[4:]) == (0xDE, 0x11, 0x33, 255)


def test_a_one_bit_png_scales_to_the_full_range(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.decode_png(build_png(2, 1, 1, 0, b"\x00\x40"))
    assert tuple(raster.pixels[:4]) == (0, 0, 0, 255)
    assert tuple(raster.pixels[4:]) == (255, 255, 255, 255)


def test_every_row_filter_is_undone(nodes) -> None:
    """Real PNGs pick a filter per row; a decoder that only knew `None` would read noise."""
    imaging, _ = nodes
    size = 6
    raster = imaging.Raster(
        size, size, make_pixels(size, size, lambda x, y: (x * 30, y * 30, (x + y) * 20, 255))
    )
    plain = imaging.encode_png(raster)
    original = imaging.decode_png(plain)

    stride = size * 4
    flat = bytes(raster.pixels)
    for kind in (0, 1, 2, 3, 4):
        rows = bytearray()
        previous = bytes(stride)
        for row in range(size):
            line = flat[row * stride : (row + 1) * stride]
            rows.append(kind)
            rows += encode_row(kind, line, previous, 4)
            previous = line
        assert imaging.decode_png(build_png(size, size, 8, 6, bytes(rows))).pixels == original.pixels


def encode_row(kind: int, line: bytes, previous: bytes, bpp: int) -> bytes:
    """Apply one PNG row filter — the inverse of what the decoder has to undo."""
    out = bytearray(len(line))
    for i, value in enumerate(line):
        left = line[i - bpp] if i >= bpp else 0
        up = previous[i]
        corner = previous[i - bpp] if i >= bpp else 0
        if kind == 0:
            out[i] = value
        elif kind == 1:
            out[i] = (value - left) & 0xFF
        elif kind == 2:
            out[i] = (value - up) & 0xFF
        elif kind == 3:
            out[i] = (value - ((left + up) >> 1)) & 0xFF
        else:
            estimate = left + up - corner
            da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
            nearest = left if da <= db and da <= dc else up if db <= dc else corner
            out[i] = (value - nearest) & 0xFF
    return bytes(out)


def test_a_broken_png_says_what_is_wrong_with_it(nodes) -> None:
    imaging, _ = nodes
    with pytest.raises(imaging.ImageError):
        imaging.decode_png(b"not a png at all")
    truncated = build_png(4, 4, 8, 6, b"\x00" + b"\x00" * 16)
    with pytest.raises(imaging.ImageError, match="truncated"):
        imaging.decode_png(truncated)


def test_an_unreadable_format_names_the_button_that_fixes_it(nodes, tmp_path: Path) -> None:
    """PNG is the floor; everything else is what building the environment adds."""
    imaging, _ = nodes
    if imaging.pillow() is not None:
        pytest.skip("Pillow is installed here, so this file is readable after all")
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    with pytest.raises(imaging.ImageError, match="Build the image_ops environment"):
        imaging.read(target)


# ------------------------------------------------------------------ the script


def run_script(
    project: Path, params: dict, script: str = "remove_background.py"
) -> tuple[subprocess.CompletedProcess, dict]:
    """Drive the node the way the engine does: JSON on stdin, results in a file."""
    assets = project / "voxarium_studio" / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    output = project / "node-output.json"
    payload = {
        "inputs": {},
        "params": params,
        "resolved": {},
        "settings": {},
        "context": {"project_root": str(project), "assets_dir": str(assets)},
    }
    process = subprocess.run(
        [sys.executable, str(NODES / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(project),
        env={
            **os.environ,
            "VOXARIUM_STUDIO_OUTPUT_FILE": str(output),
            "VOXARIUM_STUDIO_PROJECT_ROOT": str(project),
            "VOXARIUM_STUDIO_ASSETS_DIR": str(assets),
        },
    )
    results = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    return process, results


@pytest.fixture
def disc_png(nodes, tmp_path: Path) -> Path:
    imaging, _ = nodes
    size = 32
    target = tmp_path / "disc.png"
    target.write_bytes(imaging.encode_png(imaging.Raster(size, size, disc_on_white(size))))
    return target


def test_the_script_writes_a_transparent_png_into_the_assets_directory(
    nodes, tmp_path: Path, disc_png: Path
) -> None:
    imaging, _ = nodes
    process, results = run_script(tmp_path, {"image": str(disc_png), "tolerance": 10})
    assert process.returncode == 0, process.stderr

    written = Path(results["image"])
    assert written.parent == tmp_path / "voxarium_studio" / "assets"
    assert written.suffix == ".png"
    raster = imaging.decode_png(written.read_bytes())
    assert raster.pixels[3] == 0  # the corner
    assert raster.pixels[(16 * 32 + 16) * 4 + 3] == 255  # the enclosed white pixel
    assert results["removed_pixels"] > 0
    # Not a port: it is what the canvas component samples the eyedropper from.
    assert results["source"] == str(disc_png)


def test_two_settings_never_write_over_each_others_cutout(tmp_path: Path, disc_png: Path) -> None:
    """The §8.2 cache would otherwise hand back a path holding somebody else's cut."""
    _, first = run_script(tmp_path, {"image": str(disc_png), "tolerance": 10})
    _, second = run_script(tmp_path, {"image": str(disc_png), "tolerance": 40})
    assert first["image"] != second["image"]

    _, again = run_script(tmp_path, {"image": str(disc_png), "tolerance": 10})
    assert again["image"] == first["image"]


def test_a_bad_colour_fails_the_node_with_the_reason(tmp_path: Path, disc_png: Path) -> None:
    process, results = run_script(tmp_path, {"image": str(disc_png), "key_color": "chartreuse"})
    assert process.returncode != 0
    assert "chartreuse" in process.stderr
    assert results == {}


def test_a_missing_image_fails_before_anything_is_written(tmp_path: Path) -> None:
    process, results = run_script(tmp_path, {"image": str(tmp_path / "nothing.png")})
    assert process.returncode != 0
    assert "not found" in process.stderr
    assert results == {}


def test_the_converter_writes_a_png_of_exactly_the_same_pixels(
    nodes, tmp_path: Path, disc_png: Path
) -> None:
    """A PNG source, because it is the only one this interpreter decodes — and a fair one:
    the node re-containers whatever it is handed rather than refusing what its upstream
    happened to emit."""
    imaging, _ = nodes
    process, results = run_script(
        tmp_path, {"image": str(disc_png)}, script="jpeg_to_png.py"
    )
    assert process.returncode == 0, process.stderr

    written = Path(results["image"])
    assert written.parent == tmp_path / "voxarium_studio" / "assets"
    assert written.suffix == ".png"
    assert imaging.decode_png(written.read_bytes()).pixels == disc_on_white(32)
    assert "32x32" in process.stdout
    frames = [
        json.loads(line.removeprefix("::voxarium_studio:progress::"))
        for line in process.stdout.splitlines()
        if line.startswith("::voxarium_studio:progress::")
    ]
    assert [frame["label"] for frame in frames] == ["Reading", "Writing"]


def test_the_converted_name_follows_the_bytes_and_nothing_else(
    nodes, tmp_path: Path, disc_png: Path
) -> None:
    """Stable for one picture, distinct for another — the same rule the cutout's name obeys."""
    imaging, _ = nodes
    # The same filename on purpose: only the bytes may decide what the output is called.
    other = tmp_path / "elsewhere" / "disc.png"
    other.parent.mkdir()
    other.write_bytes(imaging.encode_png(imaging.Raster(8, 8, disc_on_white(8, radius=3.0))))

    _, first = run_script(tmp_path, {"image": str(disc_png)}, script="jpeg_to_png.py")
    _, again = run_script(tmp_path, {"image": str(disc_png)}, script="jpeg_to_png.py")
    _, second = run_script(tmp_path, {"image": str(other)}, script="jpeg_to_png.py")
    assert again["image"] == first["image"]
    assert second["image"] != first["image"]


def test_converting_a_jpeg_without_pillow_names_the_button_that_installs_it(
    nodes, tmp_path: Path
) -> None:
    """The one node here that needs the environment says so, rather than failing obscurely."""
    imaging, _ = nodes
    if imaging.pillow() is not None:
        pytest.skip("Pillow is installed here, so the JPEG decodes after all")
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(bytes((0xFF, 0xD8, 0xFF, 0xE0)) + bytes(64))

    process, results = run_script(tmp_path, {"image": str(photo)}, script="jpeg_to_png.py")
    assert process.returncode != 0
    assert "Build the image_ops environment" in process.stderr
    assert results == {}


def test_the_converter_needs_an_image_and_says_which_one_is_missing(tmp_path: Path) -> None:
    process, results = run_script(
        tmp_path, {"image": str(tmp_path / "nothing.jpg")}, script="jpeg_to_png.py"
    )
    assert process.returncode != 0
    assert "not found" in process.stderr
    assert results == {}


def test_the_script_reports_progress_and_says_what_it_did(tmp_path: Path, disc_png: Path) -> None:
    """A node that says nothing while it works is a node that looks hung."""
    process, _ = run_script(tmp_path, {"image": str(disc_png), "tolerance": 10})
    frames = [
        json.loads(line.removeprefix("::voxarium_studio:progress::"))
        for line in process.stdout.splitlines()
        if line.startswith("::voxarium_studio:progress::")
    ]
    assert [frame["label"] for frame in frames] == ["Reading", "Keying", "Antialiasing", "Writing"]
    assert [frame["percent"] for frame in frames] == sorted(frame["percent"] for frame in frames)
    assert "32x32" in process.stdout
    assert "keyed #ffffff out of" in process.stdout


# ------------------------------------------------------------------ the quadrant cut


def four_colours(width: int, height: int) -> bytearray:
    """A picture whose four quadrants are four flat colours, cut where the node cuts."""
    left, top = width // 2, height // 2
    palette = {
        (0, 0): (200, 30, 30),
        (1, 0): (30, 200, 30),
        (0, 1): (30, 30, 200),
        (1, 1): (220, 220, 30),
    }

    def colour(x: int, y: int) -> tuple[int, int, int, int]:
        return palette[(int(x >= left), int(y >= top))] + (255,)

    return make_pixels(width, height, colour)


@pytest.fixture
def quadrants_png(nodes, tmp_path: Path) -> Path:
    """9x7 — odd on both axes, so the uneven halving is exercised by default."""
    imaging, _ = nodes
    target = tmp_path / "quadrants.png"
    target.write_bytes(imaging.encode_png(imaging.Raster(9, 7, four_colours(9, 7))))
    return target


def test_a_crop_takes_the_rectangle_it_was_asked_for(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.Raster(4, 3, make_pixels(4, 3, lambda x, y: (x * 10, y * 10, 0, 255)))
    patch = raster.crop(1, 1, 2, 2)
    assert (patch.width, patch.height) == (2, 2)
    assert list(patch.pixels[:8]) == [10, 10, 0, 255, 20, 10, 0, 255]
    assert list(patch.pixels[8:]) == [10, 20, 0, 255, 20, 20, 0, 255]


def test_a_crop_reaching_past_the_edge_is_refused_rather_than_clamped(nodes) -> None:
    """Silently returning a smaller rectangle would make the four quadrants stop tiling."""
    imaging, _ = nodes
    raster = imaging.Raster(4, 4, bytearray(4 * 4 * 4))
    with pytest.raises(imaging.ImageError, match="outside"):
        raster.crop(2, 0, 3, 4)
    with pytest.raises(imaging.ImageError, match="positive size"):
        raster.crop(0, 0, 0, 4)


def test_the_quadrant_boxes_tile_the_image_with_nothing_lost(nodes) -> None:
    """Whatever the halving rounds to, the four rectangles still add back up."""
    import trim_4x4

    for width, height in ((8, 6), (9, 7), (2, 2), (1024, 3)):
        boxes = trim_4x4.boxes(width, height)
        assert sorted(boxes) == sorted(QUADRANTS)
        assert sum(box[2] * box[3] for box in boxes.values()) == width * height
        assert boxes["top_left"][:2] == (0, 0)
        assert boxes["bottom_right"] == (
            width // 2,
            height // 2,
            width - width // 2,
            height - height // 2,
        )


def test_the_trimmer_writes_four_quadrants_that_reassemble(
    nodes, tmp_path: Path, quadrants_png: Path
) -> None:
    """Each corner is one flat colour, so a crop that slipped a row would show."""
    imaging, _ = nodes
    process, results = run_script(
        tmp_path, {"image": str(quadrants_png)}, script="trim_4x4.py"
    )
    assert process.returncode == 0, process.stderr
    assert sorted(results) == sorted(QUADRANTS)

    expected = {
        "top_left": ((4, 3), (200, 30, 30)),
        "top_right": ((5, 3), (30, 200, 30)),
        "bottom_left": ((4, 4), (30, 30, 200)),
        "bottom_right": ((5, 4), (220, 220, 30)),
    }
    for quadrant, (size, colour) in expected.items():
        written = Path(results[quadrant])
        assert written.parent == tmp_path / "voxarium_studio" / "assets"
        raster = imaging.decode_png(written.read_bytes())
        assert (raster.width, raster.height) == size, quadrant
        assert {bytes(raster.pixels[i : i + 3]) for i in range(0, len(raster.pixels), 4)} == {
            bytes(colour)
        }, quadrant


def test_the_extra_row_and_column_of_an_odd_image_go_to_the_far_side(
    nodes, tmp_path: Path, quadrants_png: Path
) -> None:
    """9x7 halves as 4|5 and 3|4 — stated in the node's description, so it is pinned."""
    imaging, _ = nodes
    _, results = run_script(tmp_path, {"image": str(quadrants_png)}, script="trim_4x4.py")
    sizes = {
        quadrant: (lambda r: (r.width, r.height))(
            imaging.decode_png(Path(results[quadrant]).read_bytes())
        )
        for quadrant in QUADRANTS
    }
    assert sizes == {
        "top_left": (4, 3),
        "top_right": (5, 3),
        "bottom_left": (4, 4),
        "bottom_right": (5, 4),
    }
    assert sum(width * height for width, height in sizes.values()) == 9 * 7


def test_the_four_quadrants_never_write_over_each_other(
    tmp_path: Path, quadrants_png: Path
) -> None:
    """One name per corner, or three of the four outputs would be the same file."""
    _, results = run_script(tmp_path, {"image": str(quadrants_png)}, script="trim_4x4.py")
    assert len(set(results.values())) == len(QUADRANTS)
    _, again = run_script(tmp_path, {"image": str(quadrants_png)}, script="trim_4x4.py")
    assert again == results  # stable for the same bytes, like every other name here


def test_an_image_too_small_to_halve_fails_with_its_size(
    nodes, tmp_path: Path
) -> None:
    """A 1-pixel column has no left and right half, and an empty crop is not an answer."""
    imaging, _ = nodes
    sliver = tmp_path / "sliver.png"
    sliver.write_bytes(imaging.encode_png(imaging.Raster(1, 8, bytearray(32))))

    process, results = run_script(tmp_path, {"image": str(sliver)}, script="trim_4x4.py")
    assert process.returncode != 0
    assert "1x8" in process.stderr
    assert results == {}


def test_the_trimmer_needs_an_image_and_says_which_one_is_missing(tmp_path: Path) -> None:
    process, results = run_script(
        tmp_path, {"image": str(tmp_path / "nothing.png")}, script="trim_4x4.py"
    )
    assert process.returncode != 0
    assert "not found" in process.stderr
    assert results == {}


def test_the_trimmer_reports_one_progress_frame_per_quadrant(
    tmp_path: Path, quadrants_png: Path
) -> None:
    process, _ = run_script(tmp_path, {"image": str(quadrants_png)}, script="trim_4x4.py")
    frames = [
        json.loads(line.removeprefix("::voxarium_studio:progress::"))
        for line in process.stdout.splitlines()
        if line.startswith("::voxarium_studio:progress::")
    ]
    assert [frame["label"] for frame in frames] == ["Reading"] + ["Writing"] * 4
    assert [frame["percent"] for frame in frames] == sorted(f["percent"] for f in frames)
    assert [frame.get("step") for frame in frames[1:]] == [1, 2, 3, 4]
    assert "9x7 -> four quadrants" in process.stdout


# ------------------------------------------------------------------ the alpha crop

SUBJECT = (200, 60, 30)
"""The colour of the opaque patch below, so a test can say "this pixel is the subject"."""

BOX = (4, 3, 5, 6)
"""Where that patch sits in a 16x12 picture: ``(left, top, width, height)``."""


def patch_on_transparent(width: int = 16, height: int = 12, box=BOX) -> bytearray:
    """One opaque rectangle floating in transparency — the picture this node is for."""
    left, top, box_width, box_height = box

    def colour(x: int, y: int) -> tuple[int, int, int, int]:
        inside = left <= x < left + box_width and top <= y < top + box_height
        return SUBJECT + (255,) if inside else (0, 0, 0, 0)

    return make_pixels(width, height, colour)


def core_in_a_halo(width: int = 16, height: int = 12) -> bytearray:
    """A solid core inside a barely-there halo — what an antialiased cut actually leaves."""

    def colour(x: int, y: int) -> tuple[int, int, int, int]:
        if 7 <= x < 9 and 5 <= y < 7:
            return SUBJECT + (255,)
        if 5 <= x < 11 and 3 <= y < 9:
            return SUBJECT + (20,)
        return (0, 0, 0, 0)

    return make_pixels(width, height, colour)


@pytest.fixture
def patch_png(nodes, tmp_path: Path) -> Path:
    imaging, _ = nodes
    target = tmp_path / "patch.png"
    target.write_bytes(imaging.encode_png(imaging.Raster(16, 12, patch_on_transparent())))
    return target


@pytest.fixture
def halo_png(nodes, tmp_path: Path) -> Path:
    imaging, _ = nodes
    target = tmp_path / "halo.png"
    target.write_bytes(imaging.encode_png(imaging.Raster(16, 12, core_in_a_halo())))
    return target


@pytest.fixture
def wide_png(nodes, tmp_path: Path) -> Path:
    """A 40x24 subject in a 60x40 picture — proportions that halve and quarter cleanly, so a
    shrink can be asserted exactly instead of within a rounding."""
    imaging, _ = nodes
    target = tmp_path / "wide.png"
    target.write_bytes(
        imaging.encode_png(
            imaging.Raster(60, 40, patch_on_transparent(60, 40, (10, 8, 40, 24)))
        )
    )
    return target


def cropped(nodes, results: dict):
    imaging, _ = nodes
    return imaging.decode_png(Path(results["image"]).read_bytes())


def test_the_bounding_box_is_the_smallest_rectangle_holding_every_drawn_pixel(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.Raster(16, 12, patch_on_transparent())
    assert raster.alpha_bounds() == BOX


def test_a_bounding_box_spans_islands_rather_than_taking_the_first(nodes) -> None:
    """Two blobs with nothing between them still have one box, which is what "bounding" means."""
    imaging, _ = nodes
    pixels = make_pixels(
        10, 8, lambda x, y: SUBJECT + (255 if (x, y) in {(1, 1), (8, 6)} else 0,)
    )
    assert imaging.Raster(10, 8, pixels).alpha_bounds() == (1, 1, 8, 6)


def test_a_diagonal_widens_the_box_row_by_row(nodes) -> None:
    """The per-row scan narrows its search as it goes; a diagonal is what breaks it if it stops."""
    imaging, _ = nodes
    pixels = make_pixels(10, 8, lambda x, y: SUBJECT + (255 if x == y else 0,))
    assert imaging.Raster(10, 8, pixels).alpha_bounds() == (0, 0, 8, 8)


def test_an_opaque_image_has_the_whole_picture_for_a_box(nodes) -> None:
    """A JPEG carries no alpha, so this is every photograph, and it must not be an error."""
    imaging, _ = nodes
    pixels = make_pixels(6, 5, lambda x, y: SUBJECT + (255,))
    assert imaging.Raster(6, 5, pixels).alpha_bounds() == (0, 0, 6, 5)


def test_a_fully_transparent_image_has_no_bounding_box(nodes) -> None:
    """None rather than a zero-sized rectangle: there is no answer, not a degenerate one."""
    imaging, _ = nodes
    assert imaging.Raster(8, 8, bytearray(8 * 8 * 4)).alpha_bounds() is None


def test_the_threshold_leaves_a_halo_out_of_the_box(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.Raster(16, 12, core_in_a_halo())
    assert raster.alpha_bounds(0) == (5, 3, 6, 6)  # the halo counts
    assert raster.alpha_bounds(26) == (7, 5, 2, 2)  # 10% of 255, and the halo is 20


def test_a_window_inside_the_image_is_the_plain_crop(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.Raster(16, 12, patch_on_transparent())
    patch = raster.window(*BOX)
    assert (patch.width, patch.height) == (5, 6)
    assert patch.alpha() == bytes([255] * 30)
    assert {bytes(patch.pixels[i : i + 3]) for i in range(0, len(patch.pixels), 4)} == {
        bytes(SUBJECT)
    }


def test_a_window_reaching_past_the_edge_pads_with_transparent_black(nodes) -> None:
    """Where `crop` refuses so four quadrants keep tiling, `window` pads so a frame can be
    larger than the picture — which is the ordinary case for a fixed output resolution."""
    imaging, _ = nodes
    raster = imaging.Raster(16, 12, patch_on_transparent())
    framed = raster.window(-3, -2, 24, 20)
    assert (framed.width, framed.height) == (24, 20)
    assert framed.alpha().count(255) == 30  # only the subject, nothing invented
    assert framed.alpha_bounds() == (7, 5, 5, 6)
    assert bytes(framed.pixels[:4]) == b"\x00\x00\x00\x00"


def test_a_window_entirely_off_the_image_is_empty_rather_than_an_error(nodes) -> None:
    imaging, _ = nodes
    raster = imaging.Raster(16, 12, patch_on_transparent())
    assert raster.window(100, 100, 4, 4).alpha_bounds() is None


def test_a_window_still_needs_a_positive_size(nodes) -> None:
    imaging, _ = nodes
    with pytest.raises(imaging.ImageError, match="positive size"):
        imaging.Raster(4, 4, bytearray(64)).window(0, 0, 0, 4)


def test_no_output_size_frames_the_bounding_box_itself(nodes) -> None:
    import crop

    assert crop.frame_for(BOX, 0, 0) == BOX
    assert crop.frame_for(BOX, 8, 0) == (3, 3, 8, 6)  # one side fixed, the other follows


def test_the_median_is_a_colour_that_was_there_and_the_mean_is_not(nodes) -> None:
    """Three black pixels and one white one average to 191, a grey nothing in the picture
    was. The median answers black — which is the whole reason this filter exists."""
    imaging, _ = nodes
    corner = imaging.Raster(
        2, 2, make_pixels(2, 2, lambda x, y: (255, 255, 255, 255) if (x, y) == (1, 1) else (0, 0, 0, 255))
    )
    assert tuple(corner.resample_median(1, 1).pixels) == (0, 0, 0, 255)


def triples(raster) -> set[bytes]:
    """Every RGB triple a drawn pixel of *raster* uses."""
    return {
        bytes(raster.pixels[i : i + 3])
        for i in range(0, len(raster.pixels), 4)
        if raster.pixels[i + 3]
    }


def test_a_shrink_never_invents_a_colour(nodes) -> None:
    """The palette property, and note it is asserted on whole **triples**. A per-channel
    median satisfies it channel by channel and still fails here, because the middle red and
    the middle green need not have come from the same pixel."""
    imaging, _ = nodes
    palette = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (220, 220, 30), (10, 10, 10)]
    noisy = imaging.Raster(
        64, 64, make_pixels(64, 64, lambda x, y: palette[(x * 7 + y * 13) % 5] + (255,))
    )
    assert triples(noisy.resample_median(8, 8)) <= {bytes(colour) for colour in palette}


def test_a_footprint_straddling_two_colours_answers_with_one_of_them(nodes) -> None:
    """The case a per-channel median gets wrong: orange body against a blue outline would
    median to a pink in neither, because the channels are ranked independently."""
    imaging, _ = nodes
    orange, blue = (255, 128, 0), (0, 0, 255)
    stripe = imaging.Raster(
        4, 1, make_pixels(4, 1, lambda x, y: (orange if x < 2 else blue) + (255,))
    )
    assert tuple(stripe.resample_median(1, 1).pixels)[:3] in (orange, blue)


def test_a_transparent_pixel_does_not_vote_on_the_colour(nodes) -> None:
    """Straight alpha keeps a keyed-out pixel's colour, so a footprint half opaque orange
    and half transparent white must not median to something between the two — that white is
    the background the key already removed."""
    imaging, _ = nodes
    keyed = imaging.Raster(
        4, 2, make_pixels(4, 2, lambda x, y: (255, 128, 0, 255) if y == 0 else (255, 255, 255, 0))
    )
    assert tuple(keyed.resample_median(1, 1).pixels)[:3] == (255, 128, 0)


def test_a_wholly_transparent_footprint_takes_the_plain_median(nodes) -> None:
    """With nothing drawn there is no colour to prefer, and the fast path must still answer."""
    imaging, _ = nodes
    blank = imaging.Raster(2, 2, make_pixels(2, 2, lambda x, y: (10, 20, 30, 0)))
    assert tuple(blank.resample_median(1, 1).pixels) == (10, 20, 30, 0)


def test_a_resample_reduces_and_refuses_to_enlarge(nodes) -> None:
    """`fit` never asks for an enlargement, and a median has nothing to say about a pixel it
    would have to invent — so the refusal is the honest answer rather than a fallback."""
    imaging, _ = nodes
    raster = imaging.Raster(8, 8, make_pixels(8, 8, lambda x, y: (x * 8, y * 8, 0, 255)))
    assert (lambda r: (r.width, r.height))(raster.resample_median(4, 4)) == (4, 4)
    assert raster.resample_median(8, 8).pixels == raster.pixels
    with pytest.raises(imaging.ImageError, match="enlargement"):
        raster.resample_median(16, 8)
    with pytest.raises(imaging.ImageError, match="positive size"):
        raster.resample_median(0, 4)


def test_an_extreme_shrink_thins_the_footprint_rather_than_reading_all_of_it(nodes) -> None:
    """The sample cap is what keeps a hundred-fold reduction affordable. It is a regular
    grid, so the answer is still the same on every run — which §8.2's cache key needs."""
    imaging, _ = nodes
    flat = imaging.Raster(256, 256, make_pixels(256, 256, lambda x, y: (7, 9, 11, 255)))
    first = flat.resample_median(2, 2, samples=4)
    assert first.pixels == flat.resample_median(2, 2, samples=4).pixels
    assert tuple(first.pixels[:4]) == (7, 9, 11, 255)


def test_a_frame_shrinks_the_box_only_when_the_box_will_not_fit(nodes) -> None:
    import crop

    assert crop.fit(BOX, 0, 0)[2] == 1.0  # the plain trim
    assert crop.fit(BOX, 50, 50)[2] == 1.0  # room to spare on both axes
    assert crop.fit((4, 3, 200, 100), 100, 400) == (100, 400, 0.5)  # width binds
    assert crop.fit((4, 3, 200, 100), 400, 50) == (400, 50, 0.5)  # height binds
    assert crop.fit((4, 3, 200, 100), 100, 25) == (100, 25, 0.25)  # the tighter of the two


def test_a_shrink_keeps_the_proportions_and_at_least_one_pixel(nodes) -> None:
    import crop

    assert crop.shrunk_to((0, 0, 200, 100), 0.5) == (100, 50)
    assert crop.shrunk_to((0, 0, 200, 100), 1.0) == (200, 100)  # untouched, not rounded
    # A long thin subject squeezed hard would otherwise round a side to zero, and a
    # zero-sized raster is a crash rather than an answer.
    assert crop.shrunk_to((0, 0, 1000, 3), 0.001) == (1, 1)


def test_the_frame_centres_on_the_box_whether_it_pads_or_cuts(nodes) -> None:
    """One rule for both, stated as the rule: an odd difference puts the frame's centre half
    a pixel past the box's, and it goes the same way whichever rectangle is the larger."""
    import crop

    for box_side in range(1, 9):
        for out_side in range(1, 9):
            left, _, out, _ = crop.frame_for((100, 0, box_side, 1), out_side, 1)
            offset = (left + out / 2) - (100 + box_side / 2)
            assert offset == (0.0 if (out_side - box_side) % 2 == 0 else 0.5)


def test_the_cropper_trims_a_subject_out_of_its_transparent_margin(
    nodes, tmp_path: Path, patch_png: Path
) -> None:
    process, results = run_script(tmp_path, {"image": str(patch_png)}, script="crop.py")
    assert process.returncode == 0, process.stderr

    written = Path(results["image"])
    assert written.parent == tmp_path / "voxarium_studio" / "assets"
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (5, 6)
    assert raster.alpha_bounds() == (0, 0, 5, 6)  # no margin left anywhere
    assert results["bounds"] == list(BOX)
    assert results["frame"] == list(BOX)
    assert results["size"] == [5, 6]
    assert results["scale"] == 1.0
    assert results["source_size"] == [16, 12]
    # Not ports: they are what the node's own canvas component draws the box from.
    assert results["source"] == str(patch_png)


def test_a_fixed_resolution_pads_around_the_centred_box(
    nodes, tmp_path: Path, patch_png: Path
) -> None:
    _, results = run_script(
        tmp_path, {"image": str(patch_png), "width": 9, "height": 10}, script="crop.py"
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (9, 10)
    # 2 px of transparency on all four sides, and nothing but the subject inside.
    assert raster.alpha_bounds() == (2, 2, 5, 6)
    assert raster.alpha().count(255) == 30
    assert results["frame"] == [2, 1, 9, 10]


def test_a_frame_larger_than_the_picture_is_padded_rather_than_refused(
    nodes, tmp_path: Path, patch_png: Path
) -> None:
    """40 square out of a 16x12 source — the case `Raster.crop` exists to reject."""
    _, results = run_script(
        tmp_path, {"image": str(patch_png), "width": 40, "height": 40}, script="crop.py"
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (40, 40)
    assert raster.alpha_bounds() == (17, 17, 5, 6)
    assert results["frame"] == [-13, -14, 40, 40]


def test_an_odd_difference_pads_the_far_side(nodes, tmp_path: Path, patch_png: Path) -> None:
    """8 around a box of 5 cannot split evenly; the extra column goes right, as documented."""
    _, results = run_script(
        tmp_path, {"image": str(patch_png), "width": 8}, script="crop.py"
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (8, 6)  # height still follows the box
    assert raster.alpha_bounds() == (1, 0, 5, 6)  # 1 px left, 2 px right


def test_a_frame_smaller_than_the_box_shrinks_the_subject_to_fit(
    nodes, tmp_path: Path, patch_png: Path
) -> None:
    """The subject is scaled down, not cut away — and by one factor, so it keeps its shape.

    The box is 5x6 and the frame 4x4, so height binds: 4/6 shrinks it to 3x4, which then
    sits centred in the 4 wide frame with the odd column of margin on the right.
    """
    process, results = run_script(
        tmp_path, {"image": str(patch_png), "width": 4, "height": 4}, script="crop.py"
    )
    assert process.returncode == 0, process.stderr
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (4, 4)
    assert raster.alpha_bounds() == (0, 0, 3, 4)  # 3x4 of subject, one column of margin
    assert results["scale"] == pytest.approx(4 / 6)
    assert "shrunk by 0.6667 to 3x4" in process.stdout


def test_shrinking_fills_the_frame_when_the_proportions_agree(
    nodes, tmp_path: Path, wide_png: Path
) -> None:
    """A 40x24 box into a 20x12 frame is exactly half on both axes, so nothing is left over
    and nothing is lost — the case that says the subject is scaled and not cut."""
    _, results = run_script(
        tmp_path, {"image": str(wide_png), "width": 20, "height": 12}, script="crop.py"
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (20, 12)
    assert raster.alpha_bounds() == (0, 0, 20, 12)
    assert set(raster.alpha()) == {255}
    assert results["scale"] == 0.5


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ((20, 20), (0, 4, 20, 12)),  # height has slack: margin above and below
        ((30, 12), (5, 0, 20, 12)),  # width has slack: margin left and right
    ],
)
def test_the_slack_axis_becomes_margin_rather_than_stretch(
    nodes, tmp_path: Path, wide_png: Path, frame, expected
) -> None:
    """One factor for both axes. Stretching to fill would be the one framing that changes
    what the subject is, so the axis with room to spare gets transparency instead."""
    _, results = run_script(
        tmp_path,
        {"image": str(wide_png), "width": frame[0], "height": frame[1]},
        script="crop.py",
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == frame
    assert raster.alpha_bounds() == expected
    assert results["scale"] == 0.5  # the tighter axis binds in both cases


def test_a_frame_with_room_to_spare_never_resamples(
    nodes, tmp_path: Path, patch_png: Path
) -> None:
    """The pad case must stay pixel-exact: resampling a picture that already fits would cost
    it detail for nothing, so `scale` is 1 and every drawn pixel is the source's own."""
    _, results = run_script(
        tmp_path, {"image": str(patch_png), "width": 9, "height": 10}, script="crop.py"
    )
    assert results["scale"] == 1.0
    raster = cropped(nodes, results)
    assert raster.alpha_bounds() == (2, 2, 5, 6)  # the box at its original size
    drawn = {
        bytes(raster.pixels[i : i + 3])
        for i in range(0, len(raster.pixels), 4)
        if raster.pixels[i + 3]
    }
    assert drawn == {bytes(SUBJECT)}  # nothing was blended into a new colour


def test_the_threshold_reaches_the_box_the_node_actually_measures(
    nodes, tmp_path: Path, halo_png: Path
) -> None:
    _, wide = run_script(tmp_path, {"image": str(halo_png)}, script="crop.py")
    _, tight = run_script(
        tmp_path, {"image": str(halo_png), "alpha_threshold": 10}, script="crop.py"
    )
    assert wide["bounds"] == [5, 3, 6, 6]
    assert tight["bounds"] == [7, 5, 2, 2]
    assert (cropped(nodes, tight).width, cropped(nodes, tight).height) == (2, 2)


def test_two_frames_never_write_over_each_others_crop(tmp_path: Path, patch_png: Path) -> None:
    """The §8.2 cache would otherwise hand back a path holding somebody else's rectangle."""
    _, first = run_script(tmp_path, {"image": str(patch_png), "width": 9}, script="crop.py")
    _, second = run_script(tmp_path, {"image": str(patch_png), "width": 20}, script="crop.py")
    _, thresholded = run_script(
        tmp_path, {"image": str(patch_png), "width": 9, "alpha_threshold": 40}, script="crop.py"
    )
    assert len({first["image"], second["image"], thresholded["image"]}) == 3

    _, again = run_script(tmp_path, {"image": str(patch_png), "width": 9}, script="crop.py")
    assert again["image"] == first["image"]  # stable for the same bytes and the same settings


def test_a_fully_transparent_image_fails_with_what_would_fix_it(
    nodes, tmp_path: Path
) -> None:
    imaging, _ = nodes
    empty = tmp_path / "empty.png"
    empty.write_bytes(imaging.encode_png(imaging.Raster(8, 8, bytearray(8 * 8 * 4))))

    process, results = run_script(tmp_path, {"image": str(empty)}, script="crop.py")
    assert process.returncode != 0
    assert "no bounding box" in process.stderr
    assert results == {}


def test_a_negative_side_is_refused_rather_than_clamped(
    tmp_path: Path, patch_png: Path
) -> None:
    process, results = run_script(
        tmp_path, {"image": str(patch_png), "height": -5}, script="crop.py"
    )
    assert process.returncode != 0
    assert "Height cannot be negative" in process.stderr
    assert results == {}


def test_the_cropper_needs_an_image_and_says_which_one_is_missing(tmp_path: Path) -> None:
    process, results = run_script(
        tmp_path, {"image": str(tmp_path / "nothing.png")}, script="crop.py"
    )
    assert process.returncode != 0
    assert "not found" in process.stderr
    assert results == {}


def test_an_opaque_source_is_framed_and_says_there_was_nothing_to_trim(
    nodes, tmp_path: Path, disc_png: Path
) -> None:
    """`disc_png` is opaque throughout, which is every JPEG and most flattened PNGs."""
    process, results = run_script(tmp_path, {"image": str(disc_png)}, script="crop.py")
    assert process.returncode == 0, process.stderr
    assert results["bounds"] == [0, 0, 32, 32]
    assert (cropped(nodes, results).width, cropped(nodes, results).height) == (32, 32)
    assert "no margin to trim" in process.stdout


def test_the_cropper_reports_progress_and_says_what_it_measured(
    tmp_path: Path, patch_png: Path
) -> None:
    process, _ = run_script(
        tmp_path, {"image": str(patch_png), "width": 9, "height": 10}, script="crop.py"
    )
    frames = [
        json.loads(line.removeprefix("::voxarium_studio:progress::"))
        for line in process.stdout.splitlines()
        if line.startswith("::voxarium_studio:progress::")
    ]
    assert [frame["label"] for frame in frames] == ["Reading", "Measuring", "Writing"]
    assert [frame["percent"] for frame in frames] == sorted(f["percent"] for f in frames)
    assert "bounding box: 5x6 at (4,3)" in process.stdout
    assert "framed 9x10, the box centred in it" in process.stdout


def test_a_shrink_reports_a_stage_of_its_own(tmp_path: Path, wide_png: Path) -> None:
    """It is the one part of this node that is not instant, so it says when it starts."""
    process, _ = run_script(
        tmp_path, {"image": str(wide_png), "width": 20, "height": 12}, script="crop.py"
    )
    frames = [
        json.loads(line.removeprefix("::voxarium_studio:progress::"))
        for line in process.stdout.splitlines()
        if line.startswith("::voxarium_studio:progress::")
    ]
    assert [frame["label"] for frame in frames] == [
        "Reading",
        "Measuring",
        "Shrinking",
        "Writing",
    ]
    assert frames[2]["detail"] == "40x24 -> 20x12"


def test_the_frame_keeps_only_the_box_so_the_threshold_actually_removes_the_halo(
    nodes, tmp_path: Path, halo_png: Path
) -> None:
    """Padding places the *box*, never a window of the source around it. Otherwise a
    threshold raised to be rid of a halo would measure past it and then pad it right back in.
    """
    _, results = run_script(
        tmp_path,
        {"image": str(halo_png), "alpha_threshold": 10, "width": 8, "height": 8},
        script="crop.py",
    )
    raster = cropped(nodes, results)
    assert (raster.width, raster.height) == (8, 8)
    assert results["bounds"] == [7, 5, 2, 2]  # the core alone
    assert raster.alpha_bounds() == (3, 3, 2, 2)  # centred, and the halo is not there
    assert raster.alpha().count(0) == 60  # everything but the four core pixels

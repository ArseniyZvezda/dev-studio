"""image_ops — small 2D image nodes, starting with the background key.

``python_script`` rather than ``python_function``: keying a 4096-square texture is
seconds of straight-line byte work, and the §2.1 rule is that anything heavy runs out of
process so it cannot stall the event loop. The venv it declares is optional in practice —
PNG needs nothing but :mod:`zlib`, and the runner falls back to the Studio interpreter
when a plugin's environment has not been built — so the node works the moment the plugin
is enabled, and building the environment is what adds the other containers.
"""

from __future__ import annotations

from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
)

CATEGORY = "Image"
ACCENT = "#f0883e"

READABLE = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tga", ".tif", ".tiff"]
"""What the file picker offers. A hint, not a gate: PNG is the only one guaranteed
without the plugin environment, and the node says so rather than the picker hiding it."""

JPEG = [".jpg", ".jpeg", ".jpe", ".jfif"]
"""What the JPEG converter's picker offers. Every spelling of the one container, and a
hint like `READABLE` rather than a gate — the node converts whatever Pillow opens."""

REGIONS = ["edges", "everywhere"]
"""Where a matching pixel counts as background, ordered with the default first.

``edges`` floods inward from the border and only takes matching pixels it can reach, so
white *inside* the subject — an eye highlight, the hole in a letter — survives. ``everywhere``
takes every matching pixel wherever it is, which is what a sprite sheet on a flat backdrop
wants. Duplicated in ``nodes/keying.py``: a node script runs out of process and cannot
import this module."""


class RemoveBackgroundNode(BaseNode):
    """Magic-wand key on one colour, out as a PNG with an alpha channel.

    Every port is a param rather than a plugin setting, and deliberately: params are what
    the §8.2 cache key hashes, so nudging the tolerance re-runs the node instead of handing
    back the previous cutout.
    """

    schema = NodeTypeSchema(
        id="image_ops.remove_background",
        name="Remove Background",
        category=CATEGORY,
        color=ACCENT,
        icon="wand",
        version="1.0.0",
        description=(
            "Keys out one colour — white by default — the way a magic wand does, and "
            "writes a PNG with a real alpha channel. Pick the colour off the image with "
            "the eyedropper on the node."
        ),
        inputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                required=True,
                accept=READABLE,
                description=(
                    "The picture to cut out. PNG is read with no dependencies; the other "
                    "formats need this plugin's environment, which the Studio builds by itself."
                ),
            ),
            PortSchema(
                id="key_color",
                label="Key colour",
                type="color",
                required=False,
                default="#FFFFFF",
                connectable=False,
                description="The background colour to remove. Use the eyedropper on the node "
                "to take it off the image itself.",
            ),
            PortSchema(
                id="tolerance",
                label="Tolerance",
                type="number",
                required=False,
                default=12,
                minimum=0,
                maximum=100,
                step=1,
                connectable=False,
                description=(
                    "How far a pixel may differ from the key colour and still count as "
                    "background, as a percentage of the largest possible difference on any "
                    "one channel. 0 keys only the exact colour; a photographed white sheet "
                    "usually wants 10–20."
                ),
            ),
            PortSchema(
                id="region",
                label="Region",
                type="enum",
                required=False,
                default="edges",
                options=REGIONS,
                connectable=False,
                description=(
                    "'edges' floods in from the border, so matching colour enclosed by the "
                    "subject stays. 'everywhere' takes every matching pixel in the image."
                ),
            ),
            PortSchema(
                id="edge_sharpness",
                label="Edge sharpness",
                type="number",
                required=False,
                default=80,
                minimum=0,
                maximum=100,
                step=1,
                connectable=False,
                description=(
                    "How abruptly the cut happens across the tolerance band. 100 is a hard "
                    "threshold; lower values fade the alpha out over colours approaching the "
                    "key, which is what saves a soft shadow or a blurred edge."
                ),
            ),
            PortSchema(
                id="antialias",
                label="Antialias",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description=(
                    "Smooth the cut edge afterwards, so it does not read as a staircase at "
                    "100%. Costs half a pixel of coverage on each side."
                ),
            ),
        ],
        outputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                accept=[".png"],
                description="The cutout: PNG, RGBA, straight (un-premultiplied) alpha.",
            )
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/remove_background.py",
            env="plugin_venv",
            resource_class="cpu",
        ),
    )


class JpegToPngNode(BaseNode):
    """A JPEG in, the same picture out as a PNG. One port each way and nothing to tune.

    It ships no canvas component on purpose. The node has no setting worth aiming and no
    result worth looking at — the picture is the one that went in — so the schema-driven
    `GenericNode` is exactly right, and what the user gets is a small node that stays out
    of the way in the middle of a chain.
    """

    schema = NodeTypeSchema(
        id="image_ops.jpeg_to_png",
        name="JPEG to PNG",
        category=CATEGORY,
        color=ACCENT,
        icon="file-output",
        version="1.0.0",
        description=(
            "Re-containers a JPEG as a PNG, pixel for pixel, for the nodes downstream that "
            "want one — including this plugin's own, which reads PNG with nothing installed. "
            "Needs this plugin's environment, which the Studio builds by itself and waits "
            "for: JPEG is the one format image_ops will not decode itself."
        ),
        inputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                required=True,
                accept=JPEG,
                description=(
                    "The JPEG to convert. Any other container Pillow opens is converted "
                    "too — the picker's list is what the node is *for*, not what it takes."
                ),
            )
        ],
        outputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                accept=[".png"],
                description=(
                    "The same picture as a PNG, RGBA — opaque throughout, since a JPEG "
                    "carries no alpha to keep."
                ),
            )
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/jpeg_to_png.py",
            env="plugin_venv",
            resource_class="cpu",
        ),
    )


class Trim4x4Node(BaseNode):
    """One picture in, its four quadrants out on four ports.

    Four outputs rather than one folder of tiles, and that is the whole design: a port
    each is what lets the four go to four different places on the canvas — three of them
    somewhere and the fourth nowhere — where a directory would make every consumer
    re-derive which file was which corner.

    Nothing to tune, for the same reason `JpegToPngNode` has nothing: the only decision a
    halving could offer is *where* to cut, and anywhere but the middle is a crop rather
    than this node. It ships no canvas component either — the schema-driven `GenericNode`
    already draws four named output rows.
    """

    schema = NodeTypeSchema(
        id="image_ops.trim_4x4",
        name="Trim 4x4",
        category=CATEGORY,
        color=ACCENT,
        icon="grid",
        version="1.0.0",
        description=(
            "Cuts a picture into four along its middle — a 2x2 grid — and hands each "
            "quadrant to a port of its own, as PNG. An odd width or height gives the extra "
            "row or column to the right and bottom quadrants, so the four still reassemble "
            "into the original."
        ),
        inputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                required=True,
                accept=READABLE,
                description=(
                    "The picture to cut, at least 2x2 pixels. PNG is read with no "
                    "dependencies; the other formats need this plugin's environment, "
                    "which the Studio builds by itself."
                ),
            )
        ],
        outputs=[
            PortSchema(
                id="top_left",
                label="Top left",
                type="file",
                accept=[".png"],
                description="A PNG of the top-left quadrant.",
            ),
            PortSchema(
                id="top_right",
                label="Top right",
                type="file",
                accept=[".png"],
                description="A PNG of the top-right quadrant.",
            ),
            PortSchema(
                id="bottom_left",
                label="Bottom left",
                type="file",
                accept=[".png"],
                description="A PNG of the bottom-left quadrant.",
            ),
            PortSchema(
                id="bottom_right",
                label="Bottom right",
                type="file",
                accept=[".png"],
                description="A PNG of the bottom-right quadrant.",
            ),
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/trim_4x4.py",
            env="plugin_venv",
            resource_class="cpu",
        ),
    )


class CropNode(BaseNode):
    """Trim to what the alpha channel covers, then frame that at a fixed resolution.

    The two halves are one decision, which is why they are one node: the bounding box says
    where the subject *is*, and the output size says how much room it gets around that
    centre. Splitting them would mean a trim node whose consumer has to re-derive the box it
    already computed in order to centre anything.

    A frame with room to spare pads and places the subject pixel for pixel; only a frame the
    box will not fit in resamples, and it shrinks to fit rather than cropping the subject or
    stretching it. The filter is a median, so the result's colours are a subset of the
    source's — see `nodes/crop.py` and `imaging.Raster.resample_median`.
    """

    schema = NodeTypeSchema(
        id="image_ops.crop",
        name="Crop",
        category=CATEGORY,
        color=ACCENT,
        icon="crop",
        version="1.1.0",
        description=(
            "Trims a picture to the bounding box of its non-transparent pixels, and frames "
            "that box at whatever output resolution is asked for, always centred — padded "
            "with transparency when the frame is larger, and shrunk to fit when it is "
            "smaller. Shrinking keeps the subject's proportions and uses a median filter, "
            "so no colour comes out that was not in the picture."
        ),
        inputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                required=True,
                accept=READABLE,
                description=(
                    "The picture to trim, alpha channel and all — usually a cutout from "
                    "Remove Background. A format carrying no alpha is opaque everywhere, so "
                    "its bounding box is the whole picture and only the framing applies. "
                    "PNG is read with no dependencies; the other formats need this plugin's "
                    "environment, which the Studio builds by itself."
                ),
            ),
            PortSchema(
                id="alpha_threshold",
                label="Alpha threshold",
                type="number",
                required=False,
                default=0,
                minimum=0,
                maximum=100,
                step=1,
                connectable=False,
                description=(
                    "How opaque a pixel must be to count as part of the subject, as a "
                    "percentage. 0 takes anything that is not fully transparent; raise it a "
                    "few percent to ignore the faint halo an antialiased or softly keyed "
                    "cutout leaves around its edge."
                ),
            ),
            PortSchema(
                id="width",
                label="Width",
                type="integer",
                required=False,
                default=0,
                minimum=0,
                step=1,
                connectable=False,
                description=(
                    "Output width in pixels, or 0 for the bounding box's own — the plain "
                    "trim. A wider frame pads with transparency on both sides and does not "
                    "touch a pixel; a narrower one shrinks the subject to fit. Either way "
                    "the box stays centred."
                ),
            ),
            PortSchema(
                id="height",
                label="Height",
                type="integer",
                required=False,
                default=0,
                minimum=0,
                step=1,
                connectable=False,
                description=(
                    "Output height in pixels, or 0 for the bounding box's own. Set one side "
                    "and leave the other at 0 to fix a width while the height still follows "
                    "the subject. When the subject has to shrink, both sides shrink by the "
                    "same factor and the slack becomes margin — it is never stretched."
                ),
            ),
        ],
        outputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                accept=[".png"],
                description=(
                    "The framed cutout: PNG, RGBA, straight alpha, and transparent black "
                    "in whatever margin the box does not fill."
                ),
            )
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/crop.py",
            env="plugin_venv",
            resource_class="cpu",
        ),
    )


def register(registry: PluginRegistry) -> None:
    registry.add_node_type(RemoveBackgroundNode)
    registry.add_node_type(JpegToPngNode)
    registry.add_node_type(Trim4x4Node)
    registry.add_node_type(CropNode)

"""hunyuan3d — Tencent's Hunyuan3D, and the pipeline that carries its result into the
engine project (§8.3, §9.2).

Every node here is ``python_script``: it runs out of process in the plugin's own venv,
which is what makes conflicting heavy dependencies (torch and friends) possible.

Two generation nodes, because Hunyuan3D ships two shape checkpoints and which one ran
changes the mesh: ``hunyuan3d-dit-v2-1`` from one picture, ``hunyuan3d-dit-v2-mv`` from up
to four views of the same object. One node with three optional ports would have hidden that
choice inside an input that happened to be connected — a node type per model puts it in the
§8.2 cache key by construction, and lets each say what it needs on its own ports.
"""

from __future__ import annotations

from typing import Any

from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
)

CATEGORY = "3D Generation"
ACCENT = "#7C5CFF"


FORMATS = ["stl", "glb", "obj"]
TEXTURE_RESOLUTIONS = ["512", "768"]

IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".webp"]

DEFAULT_MODEL_PATH = "tencent/Hunyuan3D-2.1"
DEFAULT_MV_MODEL_PATH = "tencent/Hunyuan3D-2mv"
"""The multi-view checkpoint is its own repository — Hunyuan3D-2mv predates 2.1 and was
never folded into it. Duplicated in ``nodes/hunyuan3d.py``, which cannot import this module:
a node script runs out of process in the plugin's own venv."""

VIEWS = [
    ("front", "Front view", "The view the texture pass is conditioned on, and the one view every run needs."),
    ("left", "Left view", "Optional. The object's left, seen from above going clockwise from the front."),
    ("back", "Back view", "Optional, and the one that pays best — with it the model builds the back instead of imagining it."),
    ("right", "Right view", "Optional. The object's right."),
]
"""``schemas.GenerationRequest.images`` keys, in the model's own order."""

UP_AXES = ["y", "z", "x", "-y", "-z", "-x"]
"""Which axis of an incoming mesh points up, ordered with the default first.

Duplicated in ``nodes/voxelize.py``, which is where the rotations behind these names live:
a node script runs out of process in the plugin's own venv and cannot import this module.
"""


def tuning_ports() -> list[PortSchema]:
    """The rest of `schemas.GenerationRequest`, built fresh for each node that wants them.

    A function rather than a shared list because both generation nodes carry these and
    `_with_defaults` rebuilds a schema's ports from settings — two schemas holding the same
    port objects would be one aliasing bug away from a project default landing on the
    wrong node.
    """
    return [
        # These are ports rather than settings for the reason every other result-bearing
        # field is: a default lands in the node's params, and params are what the §8.2
        # cache key hashes. `face_count` in particular used to be a `hunyuan_*` setting,
        # which meant changing the decimation target silently handed back the previous mesh.
        PortSchema(
            id="face_count",
            label="Face count",
            type="integer",
            required=False,
            default=40000,
            minimum=1000,
            maximum=500000,
            step=1000,
            description=(
                "The target the mesh is decimated to before UV unwrapping. Applies to the "
                "texture pass — an untextured mesh comes back at full density."
            ),
            connectable=False,
        ),
        PortSchema(
            id="texture_resolution",
            label="Texture resolution",
            type="enum",
            required=False,
            default="512",
            options=TEXTURE_RESOLUTIONS,
            description="Per-view diffusion resolution during baking.",
            connectable=False,
        ),
        PortSchema(
            id="max_num_view",
            label="Bake views",
            type="integer",
            required=False,
            default=0,
            minimum=0,
            maximum=9,
            step=1,
            description=(
                "How many viewpoints are rendered for baking, 4–9. Leave at 0 to let the "
                "server choose by card size — the most VRAM-sensitive number there is, and "
                "on a 12 GB card 6 views turn a 25s bake into tens of minutes."
            ),
            connectable=False,
        ),
        PortSchema(
            id="num_chunks",
            label="Decode chunk size",
            type="integer",
            required=False,
            default=8000,
            minimum=1000,
            maximum=200000,
            step=1000,
            description=(
                "Batch size while decoding the volume. Lower it if the shape stage runs out "
                "of VRAM."
            ),
            connectable=False,
        ),
        PortSchema(
            id="remove_background",
            label="Remove background",
            type="boolean",
            required=False,
            default=True,
            description=(
                "Cut the subject out with rembg first. The shape model segments by alpha, so "
                "an image without one needs this."
            ),
            connectable=False,
        ),
    ]


def generation_ports(model_path_default: str) -> list[PortSchema]:
    """Everything both generation nodes ask for below their image input(s).

    Kept out of each schema literal so the fields that describe *what* to generate stay
    readable above the five that tune *how* — and so the two nodes cannot drift apart on
    the fields they genuinely share, which is all of them.
    """
    return [
        PortSchema(
            id="model_path",
            label="Checkpoint",
            type="string",
            required=False,
            default=model_path_default,
            description=(
                "Local mode only — in server mode the checkpoint is chosen when the "
                "server starts. A Hugging Face id or local path."
            ),
            connectable=False,
        ),
        PortSchema(
            id="seed",
            label="Seed",
            type="integer",
            required=False,
            default=0,
            description="Change the seed to regenerate a different variant.",
            connectable=False,
        ),
        PortSchema(
            id="steps",
            label="Steps",
            type="integer",
            required=False,
            default=30,
            minimum=1,
            maximum=100,
            step=1,
            description=(
                "Flow-matching steps. 5 for a draft, 30 for normal work; past 50 the "
                "difference stops being visible."
            ),
            connectable=False,
        ),
        PortSchema(
            id="guidance_scale",
            label="Guidance",
            type="number",
            required=False,
            default=5.0,
            minimum=0.0,
            maximum=20.0,
            step=0.5,
            description="How closely to follow the conditioning.",
            connectable=False,
        ),
        PortSchema(
            id="octree_resolution",
            label="Octree resolution",
            type="integer",
            required=False,
            default=256,
            minimum=64,
            maximum=512,
            step=64,
            description=(
                "Marching-cubes grid, 64–512. The main detail/VRAM lever — drop it to "
                "192 or 128 if generation runs out of memory."
            ),
            connectable=False,
        ),
        PortSchema(
            id="texture",
            label="Generate texture",
            type="boolean",
            required=False,
            default=False,
            description=(
                "The PBR texture pass, roughly 5–8x slower than shape alone. Needs the "
                "glb or obj format to survive the export."
            ),
            connectable=False,
        ),
        PortSchema(
            id="output_format",
            label="Format",
            type="enum",
            required=False,
            default="stl",
            options=FORMATS,
            description=(
                "The server exports glb and obj; stl is converted from glb here. A "
                "textured obj comes back as a zip of the OBJ and its maps."
            ),
            connectable=False,
        ),
    ] + tuning_ports()


def mesh_outputs() -> list[PortSchema]:
    return [
        PortSchema(
            id="mesh_file",
            label="Mesh",
            type="file",
            accept=[".stl", ".glb", ".obj", ".zip"],
        ),
        PortSchema(id="triangle_count", label="Triangles", type="integer"),
    ]


GENERATION_EXECUTION = dict(
    kind="python_script",
    env="plugin_venv",
    resource_class="gpu",
    # No engine timeout on purpose. `timeout_seconds` is a wall clock over the whole node,
    # and these nodes cannot be given a useful one: a draft is seconds, a textured run
    # behind a queue on a paging 12 GB card is tens of minutes, and any number covering the
    # second kills nothing that is actually stuck. The scripts watch the thing that does
    # distinguish them — whether the API is still answering — and `hunyuan_timeout` bounds
    # *that*. Cancel is still there for a run somebody has changed their mind about.
    timeout_seconds=None,
)


class ImageToMeshNode(BaseNode):
    """One image -> mesh. Resource class ``gpu`` so the engine serialises these (§8.2).

    Every port that changes the *result* is a node param on purpose — params are what the
    §8.2 cache key covers, so switching checkpoint, step count or format re-runs the node
    instead of handing back the previous mesh. Where Hunyuan3D runs is a plugin setting
    instead, for the same reason inverted.
    """

    schema = NodeTypeSchema(
        id="hunyuan3d.image_to_mesh",
        name="Hunyuan3D (Image → Mesh)",
        category=CATEGORY,
        color=ACCENT,
        icon="box",
        version="1.2.0",
        description="Generates a mesh from a reference image with Tencent's Hunyuan3D.",
        inputs=[
            PortSchema(
                id="image",
                label="Reference image",
                type="file",
                required=True,
                accept=IMAGE_SUFFIXES,
                description=(
                    "The conditioning input. Hunyuan3D is image-to-3D and has no text "
                    "field at all, so this is what the mesh is generated from."
                ),
            ),
        ] + generation_ports(DEFAULT_MODEL_PATH),
        outputs=mesh_outputs(),
        execution=ExecutionSpec(entry="nodes/image_to_mesh.py", **GENERATION_EXECUTION),
    )


class MultiviewToMeshNode(BaseNode):
    """Up to four views -> mesh, through ``hunyuan3d-dit-v2-mv``.

    Its own node type rather than three optional ports on the one above, because the
    checkpoint is not a detail of the input: the multi-view model gives each view its own
    positional embedding, so it *builds* the sides it was shown instead of inventing them,
    and it is a Hunyuan3D-2.0-generation model — on a single picture the 2.1 single-view
    model usually returns smoother, more detailed geometry. Which one ran therefore has to
    be visible on the canvas and hashed into the §8.2 cache key, and a node type is how
    both come for free.
    """

    schema = NodeTypeSchema(
        id="hunyuan3d.multiview_to_mesh",
        name="Hunyuan3D (Multiview → Mesh)",
        category=CATEGORY,
        color=ACCENT,
        icon="box",
        version="1.0.0",
        description=(
            "Generates a mesh from up to four views of one object with Hunyuan3D's "
            "multi-view model."
        ),
        inputs=[
            PortSchema(
                id=view,
                label=label,
                type="file",
                # Only the front is required — the server takes any one view and the model
                # is conditioned on however many arrive. See `nodes/multiview_to_mesh.py`
                # for why this node narrows that to the front specifically.
                required=view == "front",
                accept=IMAGE_SUFFIXES,
                description=description,
            )
            for view, label, description in VIEWS
        ] + generation_ports(DEFAULT_MV_MODEL_PATH),
        outputs=mesh_outputs()
        + [
            PortSchema(
                id="views_used",
                label="Views used",
                type="integer",
                description="How many of the four views the shape was actually built from.",
            )
        ],
        execution=ExecutionSpec(entry="nodes/multiview_to_mesh.py", **GENERATION_EXECUTION),
    )


class VoxelizeNode(BaseNode):
    schema = NodeTypeSchema(
        id="hunyuan3d.voxelize",
        name="Voxelization (Mesh→VOX)",
        category=CATEGORY,
        color="#5CC8FF",
        icon="grid-3x3",
        # 1.2.0 because `up_axis` changed the geometry, not just the options: a mesh that
        # used to come out lying down now stands up, and §8.2 keys cached results on this.
        # 1.3.0 for the same reason on the other axis: the .vox now carries the mesh's own
        # colours, so a cached grey model from 1.2.0 is not the answer to this question.
        version="1.3.0",
        description=(
            "Voxelises a mesh and writes a MagicaVoxel .vox file, keeping the mesh's own "
            "colours where it has any."
        ),
        inputs=[
            PortSchema(
                id="mesh_file",
                label="Mesh",
                type="file",
                required=True,
                # glb and obj because that is what Hunyuan3D's server exports; zip
                # because a textured obj arrives bundled with its maps.
                accept=[".stl", ".obj", ".glb", ".zip"],
                description=(
                    "The mesh to voxelise. Its base colour texture is sampled per voxel "
                    "where it has one — a GLB carries the image inside itself, and a "
                    "textured OBJ arrives as a zip with its maps beside it."
                ),
            ),
            PortSchema(
                id="texture",
                label="Texture",
                type="file",
                required=False,
                accept=[".png", ".jpg", ".jpeg", ".webp", ".bmp"],
                description=(
                    "Optional, and it wins over whatever the mesh carries. This is how a "
                    "mesh whose maps were never bundled gets its colours, and how one "
                    "mesh is re-skinned without exporting it again. The mesh still needs "
                    "UV coordinates for the image to land on — an STL has none. PNG "
                    "decodes with no dependencies; every other format needs Pillow in the "
                    "plugin's environment."
                ),
            ),
            PortSchema(
                id="resolution",
                label="Resolution",
                type="integer",
                default=64,
                minimum=8,
                maximum=200,
                step=8,
                connectable=False,
            ),
            PortSchema(
                id="up_axis",
                label="Up axis",
                type="enum",
                required=False,
                default="y",
                options=UP_AXES,
                description=(
                    "Which axis of the source mesh points up. MagicaVoxel is Z-up while "
                    "glTF is Y-up by its own specification, so a mesh from Hunyuan3D — "
                    "whose server exports glb, and whose stl is converted from one — "
                    "needs 'y' or it voxelises lying on its side. 'z' rotates nothing, "
                    "which is what a CAD-exported STL usually wants. A negative axis is "
                    "for a model that arrives upside down."
                ),
                connectable=False,
            ),
        ],
        outputs=[
            PortSchema(id="vox_file", label="Voxels (VOX)", type="file", accept=[".vox"]),
            PortSchema(id="voxel_count", label="Voxel count", type="integer"),
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/voxelize.py",
            env="plugin_venv",
            resource_class="cpu",
            timeout_seconds=300,
        ),
    )


def _with_defaults(node: type[BaseNode], values: dict[str, Any]) -> NodeTypeSchema:
    """Copy a node's schema with some input ports' defaults replaced.

    Port defaults are what the engine fills in when a node leaves a field alone, so a
    project-level default has to be applied *here*, at registration — a fallback inside
    the node script could never fire, the schema default having already won.

    It is also what puts these settings in the cache key: a default lands in the node's
    params, and params are hashed (§8.2). Settings that never become a port default —
    everything named ``hunyuan_*`` — are invisible to the cache by the same mechanism,
    which is correct for the transport but is why the checkpoint is a port and the
    server URL is not.
    """
    schema = node.schema
    ports = [
        port.model_copy(update={"default": values[port.id]}) if port.id in values else port
        for port in schema.inputs
    ]
    return schema.model_copy(update={"inputs": ports})


def _number(value: Any, low: float, high: float, *, integer: bool) -> Any | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    clamped = max(low, min(high, value))
    return int(clamped) if integer else float(clamped)


def _choice(value: Any, options: list[str]) -> str | None:
    # An enum whose options are numeric strings ("512") is a number in hand-written YAML.
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and value.strip().lower() in options:
        return value.strip().lower()
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def register(registry: PluginRegistry) -> None:
    # Project settings, edited on this plugin's own page (§9.4), become the defaults the
    # nodes start from. The registry is rebuilt when they are saved, so this re-runs.
    # Anything the page validates is validated again here: settings are a YAML file the
    # user may also edit by hand, and a bad value must not reach a node's schema.
    settings = registry.settings

    candidates: dict[str, Any] = {
        "steps": _number(settings.get("default_steps"), 1, 100, integer=True),
        "guidance_scale": _number(settings.get("default_guidance_scale"), 0, 20, integer=False),
        "octree_resolution": _number(
            settings.get("default_octree_resolution"), 64, 512, integer=True
        ),
        "num_chunks": _number(settings.get("default_num_chunks"), 1000, 200000, integer=True),
        "max_num_view": _number(settings.get("default_max_num_view"), 0, 9, integer=True),
        "texture_resolution": _choice(
            settings.get("default_texture_resolution"), TEXTURE_RESOLUTIONS
        ),
        "output_format": _choice(settings.get("default_output_format"), FORMATS),
        # `hunyuan_face_count` is where this lived while it was a transport setting. It
        # decimates the mesh, so it belongs in the cache key and is a port now; reading
        # the old key as the fallback keeps a project that set it from losing the value.
        "face_count": _number(
            settings.get("default_face_count", settings.get("hunyuan_face_count")),
            1000,
            500000,
            integer=True,
        ),
    }
    if isinstance(settings.get("default_texture"), bool):
        candidates["texture"] = settings["default_texture"]
    if isinstance(settings.get("default_remove_background"), bool):
        candidates["remove_background"] = settings["default_remove_background"]

    overrides = {port: value for port, value in candidates.items() if value is not None}

    # Everything above applies to both generation nodes; the checkpoint is the one field
    # that cannot, because the two models are two different downloads. A project running
    # only one of them still has the other's setting sitting at its own default.
    single = _text(settings.get("default_model_path"))
    if overrides or single:
        ImageToMeshNode.schema = _with_defaults(
            ImageToMeshNode, dict(overrides, **({"model_path": single} if single else {}))
        )
    multiview = _text(settings.get("default_mv_model_path"))
    if overrides or multiview:
        MultiviewToMeshNode.schema = _with_defaults(
            MultiviewToMeshNode, dict(overrides, **({"model_path": multiview} if multiview else {}))
        )

    resolution = _number(settings.get("default_resolution"), 8, 200, integer=True)
    if resolution is not None:
        VoxelizeNode.schema = _with_defaults(VoxelizeNode, {"resolution": resolution})

    registry.add_node_type(ImageToMeshNode)
    registry.add_node_type(MultiviewToMeshNode)
    registry.add_node_type(VoxelizeNode)
    # The page is also declared in plugin.yaml; registering it here keeps the code path
    # from §9.2 exercised and is idempotent.
    registry.add_page(
        id="hunyuan3d-settings",
        title="Hunyuan3D Settings",
        route="/plugins/hunyuan3d/settings",
        icon="box",
        component="frontend/SettingsPage.js",
    )

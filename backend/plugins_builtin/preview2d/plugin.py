"""The Preview 2D node — the Explorer's image pane, moved onto the pipeline canvas.

The flat counterpart of ``preview3d``, and the difference between the two is worth stating
because it looks like an inconsistency: that node ships no viewer *on principle*, since a
mesh viewer would put a 3D dependency in the Studio core. An image has no such problem —
``/api/explorer/file`` already answers with a data URL and the preview pane already paints
it — so this node draws one directly, and only asks the host for a registered previewer
first, for the formats where a plugin does know better (§6.2).

What is left on this side is the part the browser cannot do safely: turn whatever the port
carries into one real path inside the managed project, and refuse anything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeContext,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
)

CATEGORY_PREVIEW = "Preview"

IMAGE_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico"]
"""What the port advertises, matching the Explorer's own list. It is a hint for the file
picker rather than a gate — a format some plugin claims later previews here too."""


def _resolve_under_project(ctx: NodeContext, raw: str) -> Path:
    """Keep file nodes inside the managed project — they run with the server's rights.

    The same guard `core`'s file nodes use, copied rather than imported: a plugin's modules
    do not outlive its load (`.claude/rules/plugins.md`), so there is no `core` to import
    from, and one shared spelling of the check matters more than one shared function.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = ctx.project_root / path
    resolved = path.resolve()
    root = ctx.project_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Path escapes the managed project: {raw}")
    return resolved


class Preview2DNode(BaseNode):
    """Shows an image on the canvas, and hands it on unchanged."""

    schema = NodeTypeSchema(
        id="preview2d.view",
        name="Preview 2D",
        category=CATEGORY_PREVIEW,
        description=(
            "Shows an image on the canvas — the reference a generator was given, or the "
            "texture it came back with — without leaving the pipeline to go and look. The "
            "image is passed straight through, so the node can sit inline in a chain "
            "rather than only at the end of one."
        ),
        icon="image",
        color="#39c5cf",
        inputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                required=True,
                accept=IMAGE_SUFFIXES,
                description=(
                    "Relative to the project root, or absolute inside it. A path typed "
                    "here instead of connected is previewed without running anything."
                ),
            )
        ],
        outputs=[
            PortSchema(
                id="image",
                label="Image",
                type="file",
                description="The same file, so a preview can be dropped into a chain.",
            )
        ],
        execution=ExecutionSpec(
            kind="python_function",
            entry="",
            resource_class="io",
            # The node produces nothing, so a cached result would only mean the canvas keeps
            # pointing at a file some other node has since rewritten underneath it (§8.2).
            cacheable=False,
        ),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("image", "") or "").strip()
        if not raw:
            raise ValueError("No image provided")
        path = _resolve_under_project(ctx, raw)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        await ctx.log(f"previewing {path} ({path.stat().st_size} bytes)")
        # Absolute on purpose. `ExplorerService.resolve()` joins whatever it is given onto
        # the project root, and joining an absolute path inside the project yields that same
        # path — so the browser can send this back unchanged to fetch the preview, without
        # having to know where the project root is.
        return {"image": str(path)}


def register(registry: PluginRegistry) -> None:
    registry.add_node_type(Preview2DNode)

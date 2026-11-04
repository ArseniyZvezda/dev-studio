"""The Preview 3D node — the Explorer's viewport, moved onto the pipeline canvas.

Almost all of this node is its frontend, and deliberately so. The interesting decision is
recorded in ``plugin.yaml``: the node mounts whatever previewer a plugin registered for the
incoming file's suffix (§6.2) rather than carrying a viewer of its own, which is what makes
"the same viewport as the Explorer" literally true and keeps the Studio core free of any 3D
dependency.

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

MODEL_SUFFIXES = [".voxarium", ".vox", ".fbx", ".glb", ".gltf", ".stl", ".obj", ".ply", ".dae"]
"""What the port advertises. It is a hint for the file picker, not a gate: which of these
actually renders depends on the previewer plugins installed, and the node says so itself."""


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


class Preview3DNode(BaseNode):
    """Shows a 3D file on the canvas, and hands it on unchanged."""

    schema = NodeTypeSchema(
        id="preview3d.view",
        name="Preview 3D",
        category=CATEGORY_PREVIEW,
        description=(
            "Shows a 3D file on the canvas in the Explorer's own viewport — orbit, pan and "
            "zoom inside the node. The viewer comes from whichever plugin claimed the file "
            "type, so the formats it covers grow with the plugins installed. The file is "
            "passed straight through, so the node can sit inline in a pipeline rather than "
            "only at the end of one."
        ),
        icon="eye",
        color="#e066b0",
        inputs=[
            PortSchema(
                id="file",
                label="File",
                type="file",
                required=True,
                accept=MODEL_SUFFIXES,
                description=(
                    "Relative to the project root, or absolute inside it. Previewed with "
                    "whatever viewer a plugin registered for that file type. A path typed "
                    "here instead of connected is previewed without running anything."
                ),
            )
        ],
        outputs=[
            PortSchema(
                id="file",
                label="File",
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
        raw = str(ctx.get("file", "") or "").strip()
        if not raw:
            raise ValueError("No file provided")
        path = _resolve_under_project(ctx, raw)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        await ctx.log(f"previewing {path} ({path.stat().st_size} bytes)")
        # Absolute on purpose. `ExplorerService.resolve()` joins whatever it is given onto
        # the project root, and joining an absolute path inside the project yields that same
        # path — so the browser can send this back unchanged to fetch the preview, without
        # having to know where the project root is.
        return {"file": str(path)}


def register(registry: PluginRegistry) -> None:
    registry.add_node_type(Preview3DNode)

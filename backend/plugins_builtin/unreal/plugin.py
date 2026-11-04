"""unreal — drive an Unreal Engine project from a pipeline (§8.3).

The two nodes that reach the editor are ``python_script``: they run out of process, and
the work they do is mostly waiting on a socket or a subprocess, so they take the ``io``
resource class rather than ``cpu``. ``unreal.string_ue_case`` is the exception and never
touches an editor at all — it is string work, so it is ``python_function`` and runs
in-process like the ``core.*`` nodes.

No isolated venv is declared — the transport is stdlib only (sockets, subprocess), and
the code that needs Unreal's ``unreal`` module runs *inside the editor*, never here.
"""

from __future__ import annotations

import re
from typing import Any

from voxarium_studio.plugin_api import (
    BaseNode,
    ExecutionSpec,
    NodeContext,
    NodeTypeSchema,
    PluginRegistry,
    PortSchema,
)

CATEGORY = "Unreal Engine"
ACCENT = "#4FC3F7"

TRANSPORTS = ["auto", "remote", "commandlet"]

# What the editor's import factories actually accept. Note the reference pipeline's own
# .vox/.stl output is not on it — convert to FBX/OBJ/glTF before this node.
IMPORTABLE = [
    ".fbx", ".obj", ".abc", ".gltf", ".glb", ".usd", ".usda", ".usdc",
    ".png", ".jpg", ".jpeg", ".tga", ".exr", ".bmp", ".hdr",
    ".wav", ".mp3", ".ogg",
]


def _transport_port() -> PortSchema:
    return PortSchema(
        id="transport",
        label="Transport",
        type="enum",
        required=False,
        default="auto",
        options=TRANSPORTS,
        connectable=False,
        description=(
            "auto: talk to a running editor, otherwise launch a headless one. "
            "remote: require a running editor. commandlet: always headless."
        ),
    )


class ImportAssetNode(BaseNode):
    """Import a file into the Unreal project's content browser via ``AssetImportTask``."""

    schema = NodeTypeSchema(
        id="unreal.import_asset",
        name="Unreal Import Asset",
        category=CATEGORY,
        color=ACCENT,
        icon="import",
        version="1.0.0",
        description=(
            "Imports a file into the Unreal project through the editor's Python API "
            "(unreal.AssetImportTask). Outputs the content paths Unreal created."
        ),
        inputs=[
            PortSchema(
                id="file",
                label="Source file",
                type="file",
                required=True,
                accept=IMPORTABLE,
                description="Absolute path on the machine running the editor.",
            ),
            PortSchema(
                id="destination_path",
                label="Content path",
                type="string",
                required=True,
                default="/Game/VoxariumStudio",
                description="Unreal content path, e.g. /Game/Meshes. Not a disk path.",
            ),
            PortSchema(
                id="asset_name",
                label="Asset name",
                type="string",
                required=False,
                default="",
                description="Defaults to the source file's stem.",
                connectable=False,
            ),
            PortSchema(
                id="replace_existing",
                label="Replace existing",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
            ),
            PortSchema(
                id="save",
                label="Save after import",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description="Write the .uasset to disk. Required for a headless run to persist.",
            ),
            PortSchema(
                id="import_as_skeletal",
                label="Import FBX as skeletal",
                type="boolean",
                required=False,
                default=False,
                connectable=False,
            ),
            PortSchema(
                id="import_materials",
                label="Import materials",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
            ),
            _transport_port(),
        ],
        outputs=[
            PortSchema(
                id="asset_paths",
                label="Imported assets",
                type="json",
                description="Content paths Unreal reported, e.g. ['/Game/Meshes/SM_Rock'].",
            ),
            PortSchema(id="asset_path", label="First asset", type="string"),
            PortSchema(id="asset_count", label="Count", type="integer"),
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/import_asset.py",
            resource_class="io",
            timeout_seconds=1800,
            # An import mutates the project, and the editor may have been told to
            # overwrite something since — never silently skip it.
            cacheable=False,
        ),
    )


class RunPythonNode(BaseNode):
    """Run arbitrary Python inside the editor.

    Exposed because it *is* the import node's own mechanism: hiding it would only mean a
    new plugin every time a project needs a different editor call.
    """

    schema = NodeTypeSchema(
        id="unreal.run_python",
        name="Unreal Run Python",
        category=CATEGORY,
        color=ACCENT,
        icon="terminal",
        version="1.0.0",
        description=(
            "Executes a Python snippet in the Unreal editor. Print "
            "'::voxarium_studio:ue::<json object>' to return structured output to the pipeline."
        ),
        inputs=[
            PortSchema(
                id="script",
                label="Script",
                type="text",
                required=True,
                description="Python running inside the editor, where `unreal` is importable.",
            ),
            _transport_port(),
        ],
        outputs=[
            PortSchema(id="result", label="Result", type="json"),
            PortSchema(id="output", label="Editor log", type="string"),
        ],
        execution=ExecutionSpec(
            kind="python_script",
            entry="nodes/run_python.py",
            resource_class="io",
            timeout_seconds=1800,
            cacheable=False,
        ),
    )


# What separates one word from the next on the way in. Anything that is not a letter or a
# digit: `_`, `-`, a space, the dot in front of a suffix. Unreal asset names may hold none
# of them but the underscore, and that one is what the words are joined back with.
_WORD_BREAK = re.compile(r"[^0-9A-Za-z]+")


def drop_extension(name: str) -> str:
    """``name`` without its final suffix — the last one only, and a leading dot is not one.

    The same rule ``core.pathname`` applies, and deliberately the same code rather than a
    shared import: a plugin cannot import another plugin's module, and copying six lines is
    cheaper than a helper in the host for a rule `pathlib` already spells out.
    """
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def final_segment(text: str) -> str:
    """The tail of ``text`` if it is path-shaped, and ``text`` itself if it is not.

    Reads both separators, because what arrives here is usually a *file*: an upstream node
    that resolved one answers with ``str(Path)``, which on this platform is backslashed.
    Splitting on ``/`` alone would take the whole of ``D:\\Assets\\hero.png`` for a name and
    then spell it ``D_Assets_Hero``.
    """
    trimmed = text.strip().rstrip("/\\")
    if not trimmed:
        return ""
    return trimmed.replace("\\", "/").rsplit("/", 1)[-1]


def ue_case(text: str, *, prefix: str = "", remove_extension: bool = True) -> str:
    """``text`` as an Unreal asset name, or ``""`` when there is no name left in it.

    Only the *first* letter of each word is touched, so an acronym someone wrote as one
    survives (``NORMAL`` stays ``NORMAL``, not ``Normal``) and so does the hump in a name
    that was already camel-cased. Lowering the rest would be a second decision, and the one
    people notice when it is wrong.

    The prefix is kept exactly as typed apart from the underscores around it, since the
    convention has mixed-case entries — ``MI_``, ``WBP_``, ``SKM_`` — and correcting one of
    those would be guessing at a vocabulary this node does not hold.
    """
    name = final_segment(text)
    if remove_extension:
        name = drop_extension(name)
    words = [word for word in _WORD_BREAK.split(name) if word]
    body = "_".join(word[0].upper() + word[1:] for word in words)
    if not body:
        return ""
    head = prefix.strip().strip("_")
    return f"{head}_{body}" if head else body


class StringUeCaseNode(BaseNode):
    """A file name, or any string, as the asset name Unreal's conventions ask for.

    It is the piece between a generator and ``unreal.import_asset``: what comes out of the
    first is ``imagetexture_normal.jpg`` and what the content browser wants is
    ``T_Imagetexture_Normal``. String work only — nothing here opens a file or reaches an
    editor, so it can name an asset some later node is going to write.
    """

    schema = NodeTypeSchema(
        id="unreal.string_ue_case",
        name="To UE Case",
        category=CATEGORY,
        color=ACCENT,
        icon="type",
        version="1.0.0",
        description=(
            "Formats a string as an Unreal asset name: each word capitalised and joined "
            "with underscores, with an optional type prefix in front. "
            "`imagetexture_normal.jpg` with prefix `T` becomes `T_Imagetexture_Normal`."
        ),
        inputs=[
            PortSchema(
                id="text",
                label="Text",
                type="string",
                required=True,
                description=(
                    "A name, or a path to take the name from — directories are dropped and "
                    "both separators are read. Nothing has to exist; this is string work."
                ),
            ),
            PortSchema(
                id="prefix",
                label="Prefix",
                type="string",
                required=False,
                default="",
                connectable=False,
                description=(
                    "Unreal's type prefix, e.g. `T` for a texture, `SM` for a static mesh, "
                    "`MI` for a material instance. Written in front with one underscore, so "
                    "`T` and `T_` mean the same thing. Empty leaves the name unprefixed."
                ),
            ),
            PortSchema(
                id="remove_extension",
                label="Remove extension",
                type="boolean",
                required=False,
                default=True,
                connectable=False,
                description=(
                    "On by default, because an asset name never carries one: `model.fbx` "
                    "becomes `Model`. Off, the suffix is a word like any other and comes "
                    "through as `Model_Fbx`."
                ),
            ),
        ],
        outputs=[PortSchema(id="name", label="Name", type="string")],
        execution=ExecutionSpec(kind="python_function", entry="", resource_class="cpu"),
    )

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        raw = str(ctx.get("text", "") or "")
        name = ue_case(
            raw,
            prefix=str(ctx.get("prefix", "") or ""),
            remove_extension=bool(ctx.get("remove_extension", True)),
        )
        if not name:
            # Everything in it was a separator, or it was a bare suffix that the extension
            # rule then took. An empty string travelling downstream becomes an asset with
            # no name at all, which fails much further from the cause than this does.
            raise ValueError(f"Nothing left to name after formatting {raw!r}")
        return {"name": name}


def register(registry: PluginRegistry) -> None:
    registry.add_node_type(ImportAssetNode)
    registry.add_node_type(RunPythonNode)
    registry.add_node_type(StringUeCaseNode)
    registry.add_page(
        id="unreal-settings",
        title="Unreal Engine",
        route="/plugins/unreal/settings",
        icon="gamepad-2",
        component="frontend/SettingsPage.js",
    )

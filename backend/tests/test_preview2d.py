"""The Preview 2D node (§6.2).

The mirror of `test_preview3d.py`, and for the same reason: nearly all of this plugin is its
frontend, and only a browser can say whether an image actually draws. What Python owns is the
schema the canvas builds the node from, and the guard standing between a pipeline and the
rest of the disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from voxarium_studio.plugins.api import NodeContext
from voxarium_studio.plugins.registry import Registry

NODE_TYPE = "preview2d.view"


def make_ctx(project: Path, params: dict, sink: list[str] | None = None) -> NodeContext:
    async def log(message: str, stream: str = "stdout") -> None:
        (sink if sink is not None else []).append(message)

    return NodeContext(
        inputs={},
        params=params,
        project_root=project,
        assets_dir=project / "voxarium_studio" / "assets",
        work_dir=project / "voxarium_studio" / "work",
        settings={},
        log_fn=log,
    )


async def run_node(registry: Registry, project: Path, params: dict, sink=None) -> dict:
    node_class = registry.node_class(NODE_TYPE)
    assert node_class is not None, "the plugin must register an implementation, not a schema"
    return await node_class().run(make_ctx(project, params, sink))


# ------------------------------------------------------------------ the schema


def test_the_node_is_registered_with_an_image_port_each_way(registry: Registry) -> None:
    schema = registry.node_type(NODE_TYPE)
    assert schema is not None
    assert schema.plugin_id == "preview2d"
    assert schema.name == "Preview 2D"
    # Same category as Preview 3D: they are the same gesture on two kinds of artifact.
    assert schema.category == "Preview"
    assert [(port.id, port.type) for port in schema.inputs] == [("image", "file")]
    # The pass-through is what lets a preview sit inline in a chain rather than only at
    # the end of one.
    assert [(port.id, port.type) for port in schema.outputs] == [("image", "file")]
    assert schema.input("image").required
    assert ".png" in schema.input("image").accept
    assert ".jpg" in schema.input("image").accept


def test_the_node_runs_in_process_and_is_never_cached(registry: Registry) -> None:
    """It produces nothing, so a cached result would only hide a file that changed."""
    execution = registry.node_type(NODE_TYPE).execution
    assert execution.kind == "python_function"
    assert execution.resource_class == "io"
    assert execution.cacheable is False
    # Dependency-free, so it runs in the Studio's own interpreter like `core` does.
    assert execution.env is None


def test_the_plugin_ships_the_canvas_component(registry: Registry) -> None:
    """Without this the node falls back to the generic renderer and shows no viewport."""
    assert registry.node_type(NODE_TYPE).ui_component == "frontend/Preview2DNode.js"


# ------------------------------------------------------------------ run()


async def test_it_answers_with_the_absolute_path(project: Path, registry: Registry) -> None:
    """The explorer endpoints resolve an absolute path inside the project, so the browser
    can hand this straight back without knowing where the root is."""
    image = project / "Assets" / "reference.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    logs: list[str] = []

    outputs = await run_node(registry, project, {"image": "Assets/reference.png"}, logs)

    assert Path(outputs["image"]) == image.resolve()
    assert Path(outputs["image"]).is_absolute()
    assert any("reference.png" in line for line in logs)


async def test_a_path_outside_the_project_is_refused(
    project: Path, tmp_path: Path, registry: Registry
) -> None:
    """The node runs with the server's rights; the guard is what keeps it in the project."""
    outsider = tmp_path / "elsewhere.png"
    outsider.write_bytes(b"\x89PNG")

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(registry, project, {"image": str(outsider)})

    with pytest.raises(ValueError, match="escapes the managed project"):
        await run_node(registry, project, {"image": "../elsewhere.png"})


async def test_a_missing_file_is_not_silently_previewed(
    project: Path, registry: Registry
) -> None:
    with pytest.raises(FileNotFoundError):
        await run_node(registry, project, {"image": "Assets/nothing.png"})


async def test_an_empty_port_is_an_error_rather_than_an_empty_viewport(
    project: Path, registry: Registry
) -> None:
    with pytest.raises(ValueError, match="No image provided"):
        await run_node(registry, project, {"image": "   "})

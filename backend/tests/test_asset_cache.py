from __future__ import annotations

import json

import pytest

from voxarium_studio.core.paths import ProjectPaths
from voxarium_studio.engine.asset_cache import clear_unused


def test_cleanup_preserves_inputs_directories_docs_and_manual_files(tmp_path):
    paths = ProjectPaths.for_project(tmp_path)
    paths.assets.mkdir(parents=True)
    paths.pipelines.mkdir()
    paths.docs.mkdir()
    (paths.assets / "batch").mkdir()
    names = ["unused.png", "input.png", "document.png", "manual.png", "batch/frame.png"]
    for name in names:
        (paths.assets / name).write_bytes(b"1234")
    (paths.pipelines / "example.yaml").write_text(json.dumps({"nodes": [
        {"id": "a", "type": "core.path", "params": {"value": "voxarium_studio/assets/input.png"}},
        {"id": "b", "type": "core.path", "params": {"value": str(paths.assets / "batch")}}
    ]}))
    (paths.docs / "reference.md").write_text("![preview](../assets/document.png)")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"keep")
    outputs = [{"file": str(paths.assets / name)} for name in names if name != "manual.png"]
    outputs += [{"file": str(outside)}]
    result = clear_unused(paths, outputs)
    assert result == {"removed": 1, "bytes": 4, "kept": 3, "failed": 0}
    assert not (paths.assets / "unused.png").exists()
    assert all((paths.assets / name).exists() for name in names[1:])
    assert outside.exists()
    assert clear_unused(paths, outputs)["removed"] == 0


def test_invalid_definition_refuses_before_removing_any_file(tmp_path):
    paths = ProjectPaths.for_project(tmp_path)
    paths.assets.mkdir(parents=True)
    paths.pipelines.mkdir()
    output = paths.assets / "result.png"
    output.write_bytes(b"keep")
    (paths.pipelines / "bad.yaml").write_text("[broken")
    with pytest.raises(Exception):
        clear_unused(paths, [{"image": str(output)}])
    assert output.exists()


async def test_asset_cache_endpoint_refuses_active_work(ctx):
    import asyncio
    from fastapi import HTTPException
    from voxarium_studio.api.pipelines import clear_asset_cache
    gate = asyncio.Event()
    handle = ctx.jobs.submit("cache-test", "pipeline", lambda _: gate.wait())
    try:
        with pytest.raises(HTTPException) as error:
            await clear_asset_cache(ctx)
        assert error.value.status_code == 409
    finally:
        gate.set()
        await handle.task


async def test_asset_cache_endpoint_removes_recorded_outputs(ctx):
    from voxarium_studio.api.pipelines import clear_asset_cache
    from voxarium_studio.models.pipeline import NodeRun, Run
    output = ctx.paths.assets / "old.png"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(b"cache")
    async with ctx.db.session() as session:
        run = Run(pipeline_id="old")
        session.add(run)
        await session.flush()
        session.add(NodeRun(run_id=run.id, node_id="image", node_type="image.generate", outputs={"image": str(output)}))
    assert (await clear_asset_cache(ctx))["bytes"] == 5
    assert not output.exists()


def test_symlink_output_does_not_delete_target(tmp_path):
    paths = ProjectPaths.for_project(tmp_path)
    paths.assets.mkdir(parents=True)
    target = paths.assets / "original.png"
    target.write_bytes(b"keep")
    link = paths.assets / "alias.png"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert clear_unused(paths, [{"image": str(link)}])["removed"] == 0
    assert target.exists() and link.exists()

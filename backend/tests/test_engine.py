"""Pipeline execution: caching, partial runs, failure propagation, cancellation (§8.2).

Uses only the built-in ``core.*`` node types, which are in-process and stdlib-only — the
hunyuan3d nodes exercise the same engine through a subprocess and are covered in
``test_hunyuan3d.py`` without paying their runtime here.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.engine.runner import _normalise_progress
from voxarium_studio.models.pipeline import NodeRun, NodeStatus, Run, RunStatus
from voxarium_studio.schemas.pipeline import PipelineDefinition
from voxarium_studio.ws.events import EventType


def pipeline(**overrides) -> PipelineDefinition:
    """prompt -> template -> write text -> read text: real artifacts, no plugins needed."""
    definition = {
        "id": "t",
        "name": "t",
        "nodes": [
            {"id": "n1", "type": "core.prompt_input", "params": {"text": "hello"}},
            {"id": "n2", "type": "core.template", "params": {"template": "<{a}>"}},
            {"id": "n3", "type": "core.write_text_file", "params": {"path": "out.txt"}},
            {"id": "n4", "type": "core.read_text_file"},
        ],
        "edges": [
            {"from": {"node": "n1", "port": "text"}, "to": {"node": "n2", "port": "a"}},
            {"from": {"node": "n2", "port": "text"}, "to": {"node": "n3", "port": "text"}},
            {"from": {"node": "n3", "port": "file"}, "to": {"node": "n4", "port": "path"}},
        ],
    }
    definition.update(overrides)
    return PipelineDefinition.model_validate(definition)


async def node_runs(ctx: AppContext, run_id: str) -> dict[str, NodeRun]:
    async with ctx.db.session() as session:
        rows = (await session.exec(select(NodeRun).where(NodeRun.run_id == run_id))).all()
    return {row.node_id: row for row in rows}


async def load_run(ctx: AppContext, run_id: str) -> Run:
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    return run


async def test_a_run_executes_the_whole_graph(ctx: AppContext, wait_for) -> None:
    run = await ctx.runner.start(pipeline())
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    nodes = await node_runs(ctx, run.id)
    assert {n.status for n in nodes.values()} == {NodeStatus.SUCCESS}
    assert nodes["n4"].outputs["text"] == "<hello>"
    assert (ctx.paths.root / "out.txt").read_text(encoding="utf-8") == "<hello>"


async def test_statuses_and_logs_stream_as_events(ctx: AppContext, wait_for) -> None:
    subscription = ctx.bus.subscribe()
    seen: list = []

    async def drain() -> None:
        while True:
            seen.append(await subscription.get())

    collector = asyncio.create_task(drain())
    try:
        run = await ctx.runner.start(pipeline())
        await wait_for(run.id)
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()
        subscription.close()

    kinds = {event.type for event in seen}
    assert str(EventType.RUN_STARTED) in kinds
    assert str(EventType.RUN_COMPLETED) in kinds
    assert str(EventType.NODE_STATUS_CHANGED) in kinds
    assert str(EventType.NODE_LOG) in kinds


async def test_a_running_node_reports_its_progress_as_events(
    ctx: AppContext, wait_for
) -> None:
    """`core.delay` is the built-in user of `ctx.progress()`, and this is what it buys."""
    subscription = ctx.bus.subscribe()
    seen: list = []

    async def drain() -> None:
        while True:
            seen.append(await subscription.get())

    collector = asyncio.create_task(drain())
    try:
        run = await ctx.runner.start(
            pipeline(
                nodes=[{"id": "n1", "type": "core.delay", "params": {"seconds": 0.5}}],
                edges=[],
            )
        )
        await wait_for(run.id)
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()
        subscription.close()

    frames = [e.data["progress"] for e in seen if e.type == str(EventType.NODE_PROGRESS)]
    assert frames, "a node that reports progress produces node.progress events"
    assert frames[0]["label"] == "Sleeping"
    percents = [frame["percent"] for frame in frames]
    assert percents == sorted(percents) and percents[-1] >= 50
    # Not necessarily 100: `MIN_PROGRESS_INTERVAL` can drop the last frame of a node this
    # quick, and a bar the client is about to remove is the one thing safe to lose.
    assert frames[-1]["eta_s"] < frames[0]["eta_s"]
    # Every field the client is promised is present, whether or not the node sent it.
    assert set(frames[0]) == {"percent", "label", "detail", "eta_s", "step", "steps"}


def test_a_progress_frame_is_reduced_to_what_clients_are_promised() -> None:
    """It is plugin output on a channel with no back pressure, so the host decides its size."""
    frame = _normalise_progress(
        {"percent": 140, "label": " Baking ", "blob": "x" * 10_000, "step": True}
    )

    assert frame == {
        "percent": 100.0,
        "label": "Baking",
        "detail": None,
        "eta_s": None,
        "step": None,  # `True` is an int in Python and is not a step count
        "steps": None,
    }
    assert _normalise_progress({"blob": "x"}) is None, "an empty frame would blank the node"


async def test_a_rerun_reuses_cached_node_outputs(ctx: AppContext, wait_for) -> None:
    first = await ctx.runner.start(pipeline())
    await wait_for(first.id)

    second = await ctx.runner.start(pipeline())
    await wait_for(second.id)

    nodes = await node_runs(ctx, second.id)
    assert nodes["n1"].status == NodeStatus.CACHED
    assert nodes["n2"].status == NodeStatus.CACHED
    assert nodes["n1"].from_cache is True
    # write_file declares cacheable=False: it has a side effect worth repeating.
    assert nodes["n3"].status == NodeStatus.SUCCESS


async def test_changing_a_param_invalidates_the_cache(ctx: AppContext, wait_for) -> None:
    await wait_for((await ctx.runner.start(pipeline())).id)

    changed = pipeline()
    changed.node("n1").params["text"] = "different"
    run = await ctx.runner.start(changed)
    await wait_for(run.id)

    nodes = await node_runs(ctx, run.id)
    assert nodes["n1"].status == NodeStatus.SUCCESS
    assert nodes["n4"].outputs["text"] == "<different>"


async def test_use_cache_false_reruns_everything(ctx: AppContext, wait_for) -> None:
    await wait_for((await ctx.runner.start(pipeline())).id)
    run = await ctx.runner.start(pipeline(), use_cache=False)
    await wait_for(run.id)

    nodes = await node_runs(ctx, run.id)
    assert all(node.status == NodeStatus.SUCCESS for node in nodes.values())


async def test_a_partial_run_reuses_ancestors_and_reruns_downstream(ctx: AppContext, wait_for) -> None:
    await wait_for((await ctx.runner.start(pipeline())).id)

    run = await ctx.runner.start(pipeline(), start_node_id="n2")
    await wait_for(run.id)

    nodes = await node_runs(ctx, run.id)
    assert nodes["n1"].status == NodeStatus.CACHED, "ancestor resolves from cache"
    assert nodes["n2"].status == NodeStatus.SUCCESS, "the started node always re-executes"
    # Downstream is evaluated rather than skipped. It may still hit the cache — the
    # re-run produced identical inputs — which is the point of the incremental design.
    assert nodes["n4"].status in (NodeStatus.SUCCESS, NodeStatus.CACHED)
    assert nodes["n4"].status != NodeStatus.SKIPPED


async def test_a_failing_node_fails_the_run_and_skips_its_dependents(ctx: AppContext, wait_for) -> None:
    broken = PipelineDefinition.model_validate(
        {
            "id": "t",
            "name": "t",
            "nodes": [
                {"id": "a", "type": "core.read_text_file", "params": {"path": "does-not-exist.txt"}},
                {"id": "b", "type": "core.echo"},
            ],
            "edges": [{"from": {"node": "a", "port": "text"}, "to": {"node": "b", "port": "value"}}],
        }
    )
    run = await ctx.runner.start(broken)
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.ERROR
    nodes = await node_runs(ctx, run.id)
    assert nodes["a"].status == NodeStatus.ERROR
    assert nodes["a"].error
    assert nodes["b"].status == NodeStatus.SKIPPED, "a node whose upstream failed cannot run"


async def test_disabled_nodes_are_skipped(ctx: AppContext, wait_for) -> None:
    definition = pipeline()
    definition.node("n4").disabled = True
    run = await ctx.runner.start(definition)
    await wait_for(run.id)

    nodes = await node_runs(ctx, run.id)
    assert nodes["n4"].status == NodeStatus.SKIPPED
    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS


async def test_an_invalid_graph_is_refused_before_a_run_row_exists(ctx: AppContext, wait_for) -> None:
    with pytest.raises(ValueError, match="not runnable"):
        await ctx.runner.start(
            PipelineDefinition.model_validate(
                {"id": "t", "name": "t", "nodes": [{"id": "a", "type": "nope.missing"}], "edges": []}
            )
        )
    async with ctx.db.session() as session:
        assert (await session.exec(select(Run))).all() == []


async def test_cancelling_marks_the_run_and_its_unfinished_nodes(ctx: AppContext, wait_for) -> None:
    slow = PipelineDefinition.model_validate(
        {
            "id": "t",
            "name": "t",
            "nodes": [
                {"id": "a", "type": "core.delay", "params": {"seconds": 30, "value": "x"}},
                {"id": "b", "type": "core.echo"},
            ],
            "edges": [{"from": {"node": "a", "port": "value"}, "to": {"node": "b", "port": "value"}}],
        }
    )
    run = await ctx.runner.start(slow)
    for _ in range(100):
        if (await node_runs(ctx, run.id))["a"].status == NodeStatus.RUNNING:
            break
        await asyncio.sleep(0.05)

    assert await ctx.runner.cancel(run.id) is True
    await asyncio.sleep(0.5)

    assert (await load_run(ctx, run.id)).status == RunStatus.CANCELLED
    nodes = await node_runs(ctx, run.id)
    assert nodes["a"].status == NodeStatus.CANCELLED
    assert nodes["b"].status == NodeStatus.CANCELLED


async def test_cancelling_an_unknown_run_reports_nothing_to_cancel(ctx: AppContext, wait_for) -> None:
    assert await ctx.runner.cancel("run_nope") is False


async def test_a_run_writes_its_log_and_summary_to_disk(ctx: AppContext, wait_for) -> None:
    run = await ctx.runner.start(pipeline())
    await wait_for(run.id)

    run_dir = ctx.paths.run_dir(run.id)
    assert (run_dir / "run.json").exists()
    assert (run_dir / "n3.log").read_text(encoding="utf-8").strip()

"""Iterating nodes: a loop is a node plus everything to its right (§8.1/§8.2).

`core.for_each_file` is the only built-in one, and it is the whole feature: what makes a
node repeat is `NodeTypeSchema.iterates` naming an output port, and what makes a *body* is
the ordinary descendant closure of that node. So these run real pipelines rather than
poking the runner — a loop that does not write N files has not worked, whatever the
statuses say.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.engine import dag
from voxarium_studio.models.pipeline import NodeRun, NodeStatus, Run, RunStatus
from voxarium_studio.plugins.registry import Registry
from voxarium_studio.schemas.nodes import ExecutionSpec, NodeTypeSchema, PortSchema
from voxarium_studio.schemas.pipeline import PipelineDefinition
from voxarium_studio.ws.events import EventType


def copy_tree_pipeline(**overrides) -> PipelineDefinition:
    """Every file in `Assets` copied to `Export`, keeping the shape below the top folder.

    The reference use of both new nodes together: the walk hands out one path at a time,
    `core.reparent_path` turns it into where the copy goes, and the read/write pair does
    the work — once per file, with nothing in the graph saying how many times.
    """
    definition = {
        "id": "loop",
        "name": "loop",
        "nodes": [
            {
                "id": "walk",
                "type": "core.for_each_file",
                "params": {"directory": "Assets", "recursive": True},
            },
            {
                "id": "target",
                "type": "core.reparent_path",
                "params": {"parents": 1, "new_parent": "Export"},
            },
            {"id": "read", "type": "core.read_text_file"},
            {"id": "write", "type": "core.write_text_file"},
        ],
        "edges": [
            {"from": {"node": "walk", "port": "path"}, "to": {"node": "target", "port": "path"}},
            {"from": {"node": "walk", "port": "path"}, "to": {"node": "read", "port": "path"}},
            {"from": {"node": "read", "port": "text"}, "to": {"node": "write", "port": "text"}},
            {"from": {"node": "target", "port": "path"}, "to": {"node": "write", "port": "path"}},
        ],
    }
    definition.update(overrides)
    return PipelineDefinition.model_validate(definition)


def write(ctx: AppContext, relative: str, text: str) -> None:
    target = ctx.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


async def node_runs(ctx: AppContext, run_id: str) -> dict[str, NodeRun]:
    async with ctx.db.session() as session:
        rows = (await session.exec(select(NodeRun).where(NodeRun.run_id == run_id))).all()
    return {row.node_id: row for row in rows}


async def load_run(ctx: AppContext, run_id: str) -> Run:
    async with ctx.db.session() as session:
        run = await session.get(Run, run_id)
    assert run is not None
    return run


# ------------------------------------------------------------------ running a loop


async def test_the_body_runs_once_per_file(ctx: AppContext, wait_for) -> None:
    write(ctx, "Assets/a.txt", "first")
    write(ctx, "Assets/nested/b.txt", "second")

    run = await ctx.runner.start(copy_tree_pipeline())
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    assert (ctx.paths.root / "Export/a.txt").read_text(encoding="utf-8") == "first"
    assert (ctx.paths.root / "Export/nested/b.txt").read_text(encoding="utf-8") == "second"


async def test_the_iterating_node_stores_the_whole_list(ctx: AppContext, wait_for) -> None:
    """What is kept is what the node produced. The body saw one item at a time, but the
    node answered with all of them, and a run summary claiming otherwise would describe a
    single file for a loop that touched twelve."""
    write(ctx, "Assets/a.txt", "first")
    write(ctx, "Assets/b.txt", "second")

    run = await ctx.runner.start(copy_tree_pipeline())
    await wait_for(run.id)

    nodes = await node_runs(ctx, run.id)
    assert nodes["walk"].outputs["path"] == ["Assets/a.txt", "Assets/b.txt"]
    # A body node keeps its last turn — one row per node is one row per node.
    assert nodes["write"].outputs["file"].endswith("b.txt")
    assert {node.status for node in nodes.values()} == {NodeStatus.SUCCESS}


async def test_a_loop_over_nothing_skips_its_body(ctx: AppContext, wait_for) -> None:
    """Left `pending`, those nodes would sit in the canvas's still-to-come colour for a run
    that has ended."""
    (ctx.paths.root / "Assets").mkdir(exist_ok=True)

    run = await ctx.runner.start(copy_tree_pipeline())
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    nodes = await node_runs(ctx, run.id)
    assert nodes["walk"].status == NodeStatus.SUCCESS
    assert nodes["walk"].outputs["path"] == []
    assert {nodes[n].status for n in ("target", "read", "write")} == {NodeStatus.SKIPPED}


async def test_a_failing_turn_ends_the_loop(ctx: AppContext, wait_for) -> None:
    """The alternative is a run reporting success with most of its files quietly unwritten.

    `core.reparent_path` cannot remove three parents from `Assets/bb/b.txt`, so the second
    file fails — and the third never starts.
    """
    write(ctx, "Assets/aa/deeper/a.txt", "first")
    write(ctx, "Assets/bb/b.txt", "second")
    write(ctx, "Assets/cc/deeper/c.txt", "third")

    pipeline = copy_tree_pipeline()
    pipeline.node("target").params["parents"] = 3
    run = await ctx.runner.start(pipeline)
    await wait_for(run.id)

    stored = await load_run(ctx, run.id)
    assert stored.status == RunStatus.ERROR
    assert "b.txt" in (stored.error or "")

    nodes = await node_runs(ctx, run.id)
    assert nodes["target"].status == NodeStatus.ERROR
    # The loop node fails with the turn: it is the node that was running the body.
    assert nodes["walk"].status == NodeStatus.ERROR
    assert "item 2/3" in (nodes["walk"].error or "")
    # The first turn did land, and the third never began.
    assert (ctx.paths.root / "Export/a.txt").exists()
    assert not (ctx.paths.root / "Export/c.txt").exists()


async def test_a_loop_reports_which_item_it_is_on(ctx: AppContext, wait_for) -> None:
    """The iterating node stays `running` for the whole loop and carries the count on the
    ordinary progress channel, so the canvas draws a bar over the files rather than showing
    the node finished while everything below it is still going."""
    for index in range(4):
        write(ctx, f"Assets/{index}.txt", str(index))

    subscription = ctx.bus.subscribe()
    seen: list = []

    async def drain() -> None:
        while True:
            seen.append(await subscription.get())

    collector = asyncio.create_task(drain())
    try:
        run = await ctx.runner.start(copy_tree_pipeline())
        await wait_for(run.id)
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()
        subscription.close()

    frames = [
        event.data["progress"]
        for event in seen
        if event.type == str(EventType.NODE_PROGRESS) and event.data["node_id"] == "walk"
    ]
    assert frames, "the loop published no progress"
    assert all(frame["steps"] == 4 for frame in frames)
    assert frames[0]["label"] == "Assets/0.txt"

    statuses = [
        event.data["status"]
        for event in seen
        if event.type == str(EventType.NODE_STATUS_CHANGED) and event.data["node_id"] == "walk"
    ]
    assert statuses == ["running", "success"]


async def test_the_body_is_cancelled_with_the_run(ctx: AppContext, wait_for) -> None:
    """A loop is not a way to escape Cancel: the turn in flight unwinds like any node."""
    for index in range(20):
        write(ctx, f"Assets/{index}.txt", str(index))

    pipeline = copy_tree_pipeline()
    pipeline.nodes.append(
        type(pipeline.nodes[0]).model_validate(
            {"id": "slow", "type": "core.delay", "params": {"seconds": 5}}
        )
    )
    pipeline.edges.append(
        type(pipeline.edges[0]).model_validate(
            {"from": {"node": "walk", "port": "path"}, "to": {"node": "slow", "port": "value"}}
        )
    )

    run = await ctx.runner.start(pipeline)
    for _ in range(200):
        await asyncio.sleep(0.02)
        if (await node_runs(ctx, run.id))["slow"].status == NodeStatus.RUNNING:
            break
    assert await ctx.runner.cancel(run.id)
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.CANCELLED
    assert (await node_runs(ctx, run.id))["walk"].status == NodeStatus.CANCELLED


async def test_running_from_a_node_inside_a_loop_still_loops(ctx: AppContext, wait_for) -> None:
    """"Run from this node" reaches into a body: the selection pulls in the iterator as an
    ancestor, so it lists again and the part of the body that was selected repeats with
    it. Anything else would run that node once, against whichever item happened to be
    last."""
    write(ctx, "Assets/a.txt", "first")
    write(ctx, "Assets/b.txt", "second")

    run = await ctx.runner.start(copy_tree_pipeline(), start_node_id="target")
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    nodes = await node_runs(ctx, run.id)
    assert nodes["walk"].status == NodeStatus.SUCCESS
    assert nodes["walk"].outputs["path"] == ["Assets/a.txt", "Assets/b.txt"]
    # `read` is neither the started node, its descendant, nor its ancestor.
    assert nodes["read"].status == NodeStatus.SKIPPED
    assert sorted(path.name for path in (ctx.paths.root / "Export").iterdir()) == [
        "a.txt",
        "b.txt",
    ]


async def test_pausing_inside_a_loop_holds_every_turn(ctx: AppContext, wait_for) -> None:
    """The point of `pause_on_complete` is looking at a result before going on, and the
    result worth looking at in a loop is each turn's — so the hold is per turn, and
    Continue means "and the next one" rather than "and all the rest"."""
    write(ctx, "Assets/a.txt", "first")
    write(ctx, "Assets/b.txt", "second")

    pipeline = copy_tree_pipeline()
    pipeline.node("write").pause_on_complete = True
    run = await ctx.runner.start(pipeline)

    for turn in range(2):
        await wait_until_paused(ctx, run.id, ["write"])
        written = sorted(path.name for path in (ctx.paths.root / "Export").iterdir())
        assert len(written) == turn + 1, "the run went past the pause"
        assert await ctx.runner.resume(run.id)

    await wait_for(run.id)
    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    assert (await node_runs(ctx, run.id))["write"].status == NodeStatus.SUCCESS


async def wait_until_paused(
    ctx: AppContext, run_id: str, nodes: list[str], timeout: float = 20.0
) -> None:
    """Wait until the run is paused in *both* the places the assertion then reads.

    `paused_nodes` leads the stored row — `_hold` fills the live record before it awaits
    the `PAUSED` write — so watching only the live set hands back control while the row
    still says `RUNNING`. `test_pins_and_pause.py` is where that is explained at length.
    """
    for _ in range(int(timeout * 50)):
        if ctx.runner.paused_nodes(run_id) == nodes:
            if (await load_run(ctx, run_id)).status == RunStatus.PAUSED:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} never paused at {nodes}")


# ------------------------------------------------------------------ nesting


async def test_a_loop_inside_a_loop_runs_the_inner_one_whole_each_turn(
    ctx: AppContext, wait_for
) -> None:
    """Nesting needs no special case: the inner iterator is one of the outer body's nodes,
    so the outer walk hands it over as a unit and finds it again with a smaller scope.

    Every file under `Assets`, then every file sitting in *that file's own folder* — so
    `one/`'s two files are walked twice and `two/`'s one file once. Five turns of the body
    for three files, which is the arithmetic only a real nesting produces.
    """
    write(ctx, "Assets/one/a.txt", "a")
    write(ctx, "Assets/one/b.txt", "b")
    write(ctx, "Assets/two/c.txt", "c")

    pipeline = PipelineDefinition.model_validate(
        {
            "id": "nested",
            "name": "nested",
            "nodes": [
                {
                    "id": "outer",
                    "type": "core.for_each_file",
                    "params": {"directory": "Assets", "recursive": True},
                },
                {"id": "folder", "type": "core.parent_path", "params": {"levels": 1}},
                {"id": "inner", "type": "core.for_each_file"},
                {
                    "id": "target",
                    "type": "core.reparent_path",
                    "params": {"parents": 1, "new_parent": "Export"},
                },
                {"id": "read", "type": "core.read_text_file"},
                {"id": "write", "type": "core.write_text_file"},
            ],
            "edges": [
                {
                    "from": {"node": "outer", "port": "path"},
                    "to": {"node": "folder", "port": "path"},
                },
                {
                    "from": {"node": "folder", "port": "path"},
                    "to": {"node": "inner", "port": "directory"},
                },
                {
                    "from": {"node": "inner", "port": "path"},
                    "to": {"node": "target", "port": "path"},
                },
                {
                    "from": {"node": "inner", "port": "path"},
                    "to": {"node": "read", "port": "path"},
                },
                {"from": {"node": "read", "port": "text"}, "to": {"node": "write", "port": "text"}},
                {
                    "from": {"node": "target", "port": "path"},
                    "to": {"node": "write", "port": "path"},
                },
            ],
        }
    )

    subscription = ctx.bus.subscribe()
    seen: list = []

    async def drain() -> None:
        while True:
            seen.append(await subscription.get())

    collector = asyncio.create_task(drain())
    try:
        run = await ctx.runner.start(pipeline)
        await wait_for(run.id)
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()
        subscription.close()

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    exported = sorted(
        path.relative_to(ctx.paths.root).as_posix()
        for path in (ctx.paths.root / "Export").rglob("*.txt")
    )
    assert exported == ["Export/one/a.txt", "Export/one/b.txt", "Export/two/c.txt"]

    started = [
        event
        for event in seen
        if event.type == str(EventType.NODE_STATUS_CHANGED)
        and event.data["node_id"] == "write"
        and event.data["status"] == "running"
    ]
    assert len(started) == 5


# ------------------------------------------------------------------ graph shapes


def loop_pipeline(edges: list[dict]) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {
            "id": "shape",
            "name": "shape",
            "nodes": [
                {"id": "a", "type": "core.for_each_file", "params": {"directory": "Assets"}},
                {"id": "b", "type": "core.for_each_file", "params": {"directory": "Assets"}},
                {"id": "c", "type": "core.echo"},
            ],
            "edges": edges,
        }
    )


def test_two_crossing_loops_are_refused(registry: Registry) -> None:
    """A node inside two loops that are not one within the other has no answer for how
    often it runs: once per outer item, once per inner one, or once per pair."""
    pipeline = loop_pipeline(
        [
            {"from": {"node": "a", "port": "path"}, "to": {"node": "c", "port": "value"}},
            {"from": {"node": "b", "port": "path"}, "to": {"node": "c", "port": "prefix"}},
        ]
    )
    issues = dag.validate(pipeline, registry)
    assert [issue.message for issue in issues if "repeat independently" in issue.message]


def test_one_loop_inside_another_is_fine(registry: Registry) -> None:
    """The same overlap, nested — which is the shape you get by putting one loop after
    another, and the one with an answer."""
    pipeline = loop_pipeline(
        [
            {"from": {"node": "a", "port": "path"}, "to": {"node": "b", "port": "directory"}},
            {"from": {"node": "b", "port": "path"}, "to": {"node": "c", "port": "value"}},
        ]
    )
    assert [i for i in dag.validate(pipeline, registry) if i.level == "error"] == []


def test_the_body_is_everything_downstream(registry: Registry) -> None:
    pipeline = loop_pipeline(
        [{"from": {"node": "a", "port": "path"}, "to": {"node": "c", "port": "value"}}]
    )
    bodies = dag.loop_bodies(pipeline, registry, {"a", "b", "c"})
    assert bodies == {"a": {"c"}}


def test_a_disabled_loop_owns_nothing(registry: Registry) -> None:
    """It is not going to run, so its descendants belong to the ordinary walk."""
    pipeline = loop_pipeline(
        [{"from": {"node": "a", "port": "path"}, "to": {"node": "c", "port": "value"}}]
    )
    pipeline.node("a").disabled = True
    assert dag.loop_bodies(pipeline, registry, {"a", "b", "c"}) == {}


# ------------------------------------------------------------------ the schema contract


def test_the_built_in_loop_declares_its_port(registry: Registry) -> None:
    schema = registry.node_type("core.for_each_file")
    assert schema is not None
    assert schema.iterates == "path"
    assert schema.output("path") is not None


def test_iterating_over_a_port_that_is_not_an_output_is_refused() -> None:
    with pytest.raises(ValueError, match="not one of its output ports"):
        NodeTypeSchema(
            id="x.loop",
            name="Loop",
            outputs=[PortSchema(id="path")],
            iterates="item",
            execution=ExecutionSpec(kind="python_function", entry="", cacheable=False),
        )


def test_an_iterating_node_may_not_be_cacheable() -> None:
    """A cache hit answers with the list and never runs the body, which is not the same
    run — so the contradiction is refused where it is written, not at execution."""
    with pytest.raises(ValueError, match="cacheable=False"):
        NodeTypeSchema(
            id="x.loop",
            name="Loop",
            outputs=[PortSchema(id="path")],
            iterates="path",
            execution=ExecutionSpec(kind="python_function", entry=""),
        )

"""Per-node pins, and holding a run at a node (§8.1, §8.2).

Two features that share a home on `NodeInstance` and nothing else. Pins are a *document*
concern — the canvas decides which handles exist and the YAML records only what differs from
the node type's own answer — while the pause is an *engine* concern, and the engine tests
here use only `core.*` node types for the reason `testing.md` gives.
"""

from __future__ import annotations

import asyncio

import yaml
from sqlmodel import select

from voxarium_studio.core.context import AppContext
from voxarium_studio.engine.runner import _Hold
from voxarium_studio.models.pipeline import NodeRun, NodeStatus, Run, RunStatus
from voxarium_studio.plugins.registry import Registry
from voxarium_studio.schemas.nodes import PortSchema
from voxarium_studio.schemas.pipeline import NodeInstance, PipelineDefinition
from voxarium_studio.ws.events import EventType


def pipeline(**overrides) -> PipelineDefinition:
    """n1 -> n2: two in-process nodes, enough to have a "before" and an "after"."""
    definition = {
        "id": "t",
        "name": "t",
        "nodes": [
            {"id": "n1", "type": "core.prompt_input", "params": {"text": "hello"}},
            {"id": "n2", "type": "core.echo", "params": {"prefix": ">"}},
        ],
        "edges": [
            {"from": {"node": "n1", "port": "text"}, "to": {"node": "n2", "port": "value"}}
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


async def wait_until_paused(
    ctx: AppContext, run_id: str, nodes: list[str] | None = None, timeout: float = 20.0
) -> None:
    """Wait until the run is paused in *both* the places a test then reads.

    `paused_nodes` leads the stored row, and by more than a hair under load: `_hold` puts
    the node in the live record before it awaits the `PAUSED` write, the log line and the
    event. So a wait that watched only the live set could hand back control while the row
    still said `RUNNING` — which the assertion on the next line then read. That is a race
    the scheduler usually wins and does not always, and it is the whole reason this waits
    for the row too rather than for the set alone.

    `nodes` additionally pins *which* nodes are holding it, for the tests where a second
    node lands later and the answer changes from one set to another.
    """
    for _ in range(int(timeout * 50)):
        paused = ctx.runner.paused_nodes(run_id)
        if paused and (nodes is None or paused == nodes):
            if (await load_run(ctx, run_id)).status == RunStatus.PAUSED:
                return
        await asyncio.sleep(0.02)
    raise AssertionError(
        f"run {run_id} never paused at {nodes if nodes else 'any node'} "
        f"(live: {ctx.runner.paused_nodes(run_id)})"
    )


# ------------------------------------------------------------------ pins


def test_a_port_follows_its_node_type_unless_the_instance_says_otherwise() -> None:
    connectable = PortSchema(id="value", connectable=True)
    config = PortSchema(id="seconds", connectable=False)
    node = NodeInstance(id="n1", type="core.delay")

    assert node.pin_enabled(connectable) is True
    assert node.pin_enabled(config) is False

    # Both directions have to be sayable, which is why this is a map and not a list: one
    # answer means "the type is wrong for this pipeline" and the other means "I never use it".
    overridden = NodeInstance(
        id="n1", type="core.delay", pins={"seconds": True, "value": False}
    )
    assert overridden.pin_enabled(config) is True
    assert overridden.pin_enabled(connectable) is False


def test_the_yaml_says_nothing_when_there_is_nothing_to_say() -> None:
    """A file meant to be read in a diff should not carry `pins: {}` on every node."""
    plain = pipeline().to_yaml_dict()
    assert "pins" not in plain["nodes"][0]
    assert "pause_on_complete" not in plain["nodes"][0]


def test_a_divergence_survives_a_round_trip_through_yaml(tmp_path) -> None:
    definition = pipeline(
        nodes=[
            {
                "id": "n1",
                "type": "core.delay",
                "params": {"seconds": 2},
                "pins": {"seconds": True},
                "pause_on_complete": True,
            }
        ],
        edges=[],
    )
    text = yaml.safe_dump(definition.to_yaml_dict(), sort_keys=False)
    reloaded = PipelineDefinition.model_validate(yaml.safe_load(text))

    assert reloaded.nodes[0].pins == {"seconds": True}
    assert reloaded.nodes[0].pause_on_complete is True


async def test_the_engine_reads_an_edge_into_a_port_the_type_calls_config(
    ctx: AppContext, wait_for
) -> None:
    """The point of the feature: an upstream node driving a field its author made a setting.

    `core.echo`'s `prefix` is connectable, `core.delay`'s `seconds` is not — so this uses
    the one the type declares as config and connects it anyway. Nothing in the runner
    consults `connectable` at all, which is what makes the pin a purely visual promise the
    canvas has to keep; this pins the half that matters, that the value arrives.
    """
    run = await ctx.runner.start(
        pipeline(
            nodes=[
                {"id": "n1", "type": "core.prompt_input", "params": {"text": "0"}},
                {
                    "id": "n2",
                    "type": "core.delay",
                    "params": {"seconds": 30, "value": "done"},
                    "pins": {"seconds": True},
                },
            ],
            edges=[
                {
                    "from": {"node": "n1", "port": "text"},
                    "to": {"node": "n2", "port": "seconds"},
                }
            ],
        )
    )
    await wait_for(run.id, timeout=20)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    # The edge won: 30 seconds would have blown the wait.
    assert (await node_runs(ctx, run.id))["n2"].inputs["seconds"] == "0"


def test_the_registry_still_publishes_the_type_level_default(registry: Registry) -> None:
    """`connectable` is now a default rather than a rule, and it still has to be published —
    it is the answer the canvas starts from for a node nobody has configured."""
    delay = registry.node_type("core.delay")
    assert delay.input("seconds").connectable is False
    assert delay.input("value").connectable is True


# ------------------------------------------------------------------ pause on complete


async def test_a_run_stops_at_a_paused_node_and_carries_on_when_resumed(
    ctx: AppContext, wait_for
) -> None:
    definition = pipeline()
    definition.nodes[0].pause_on_complete = True

    run = await ctx.runner.start(definition)
    await wait_until_paused(ctx, run.id)

    assert ctx.runner.paused_nodes(run.id) == ["n1"]
    assert (await load_run(ctx, run.id)).status == RunStatus.PAUSED
    rows = await node_runs(ctx, run.id)
    # The node did its work — outputs and all — and is only holding the flow.
    assert rows["n1"].status == NodeStatus.PAUSED
    assert rows["n1"].outputs["text"] == "hello"
    assert rows["n1"].finished_at is not None
    # Downstream has not started, which is the whole point.
    assert rows["n2"].status == NodeStatus.PENDING

    assert await ctx.runner.resume(run.id) is True
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS
    rows = await node_runs(ctx, run.id)
    # `paused` is live state; a finished run claiming it would describe a wait nobody can end.
    assert rows["n1"].status == NodeStatus.SUCCESS
    assert rows["n2"].outputs["value"] == ">hello"
    assert ctx.runner.paused_nodes(run.id) == []


async def test_pausing_and_resuming_are_announced(ctx: AppContext, wait_for) -> None:
    subscription = ctx.bus.subscribe()
    seen: list = []

    async def drain() -> None:
        while True:
            seen.append(await subscription.get())

    collector = asyncio.create_task(drain())
    try:
        definition = pipeline()
        definition.nodes[0].pause_on_complete = True
        run = await ctx.runner.start(definition)
        await wait_until_paused(ctx, run.id)
        await ctx.runner.resume(run.id)
        await wait_for(run.id)
        await asyncio.sleep(0.1)
    finally:
        collector.cancel()
        subscription.close()

    kinds = {event.type for event in seen}
    assert str(EventType.RUN_PAUSED) in kinds
    assert str(EventType.RUN_RESUMED) in kinds
    paused = next(e for e in seen if e.type == str(EventType.RUN_PAUSED))
    assert paused.data["node_id"] == "n1"
    assert paused.data["node_ids"] == ["n1"]


async def test_resuming_a_run_that_is_not_paused_says_so(ctx: AppContext, wait_for) -> None:
    """The API turns this into a 409 rather than pretending something happened."""
    run = await ctx.runner.start(pipeline())
    await wait_for(run.id)
    assert await ctx.runner.resume(run.id) is False
    assert await ctx.runner.resume("run_nothing") is False


async def test_cancelling_a_paused_run_ends_it(ctx: AppContext, wait_for) -> None:
    definition = pipeline()
    definition.nodes[0].pause_on_complete = True

    run = await ctx.runner.start(definition)
    await wait_until_paused(ctx, run.id)
    assert await ctx.runner.cancel(run.id) is True
    await wait_for(run.id)

    assert (await load_run(ctx, run.id)).status == RunStatus.CANCELLED
    rows = await node_runs(ctx, run.id)
    # The node succeeded; the *run* was cancelled. Leaving it "paused" would outlive the
    # wait it described.
    assert rows["n1"].status == NodeStatus.SUCCESS
    assert rows["n2"].status == NodeStatus.CANCELLED
    assert ctx.runner.paused_nodes(run.id) == []


async def test_a_cached_node_pauses_too(ctx: AppContext, wait_for) -> None:
    """Iterating is exactly when the upstream is already cached, so a cache hit that ran
    straight past the stop would make the setting useless on the second run."""
    definition = pipeline()
    first = await ctx.runner.start(definition)
    await wait_for(first.id)

    definition.nodes[0].pause_on_complete = True
    second = await ctx.runner.start(definition)
    await wait_until_paused(ctx, second.id)

    rows = await node_runs(ctx, second.id)
    assert rows["n1"].status == NodeStatus.PAUSED
    assert rows["n1"].from_cache is True

    await ctx.runner.resume(second.id)
    await wait_for(second.id)

    rows = await node_runs(ctx, second.id)
    # The cache flag survives the second write; without it the node would come back looking
    # like it had run.
    assert rows["n1"].status == NodeStatus.CACHED
    assert rows["n1"].from_cache is True


async def test_one_continue_frees_every_node_holding_the_run(
    ctx: AppContext, wait_for
) -> None:
    """Two nodes in one level both stop; Continue is a statement about the run, not a node."""
    definition = PipelineDefinition.model_validate(
        {
            "id": "t",
            "name": "t",
            "nodes": [
                {
                    "id": "a",
                    "type": "core.prompt_input",
                    "params": {"text": "one"},
                    "pause_on_complete": True,
                },
                {
                    "id": "b",
                    "type": "core.prompt_input",
                    "params": {"text": "two"},
                    "pause_on_complete": True,
                },
            ],
            "edges": [],
        }
    )
    run = await ctx.runner.start(definition)
    for _ in range(1000):
        if ctx.runner.paused_nodes(run.id) == ["a", "b"]:
            break
        await asyncio.sleep(0.02)
    assert ctx.runner.paused_nodes(run.id) == ["a", "b"]

    assert await ctx.runner.resume(run.id) is True
    await wait_for(run.id)
    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS


async def test_a_node_finishing_after_a_continue_stops_the_run_again(
    ctx: AppContext, wait_for
) -> None:
    """Two stops that happen to overlap are still two stops.

    `a` pauses immediately, `b` is still sleeping; Continue frees `a`, and when `b` lands it
    has to hold the run a second time rather than inheriting the answer given to `a`.

    Ordinary ordering only — `a` has finished unwinding long before `b` arrives. The
    interleaving where it has *not* is the next test, which this one does not reach.

    `b`'s delay is the margin the test needs, not a duration it is asserting: everything
    up to the Continue has to happen while `b` is still in flight, or the two stops are
    one and there is nothing here to see. Two seconds, because the suite runs across every
    core and a worker that loses the CPU for a moment is ordinary there.
    """
    definition = PipelineDefinition.model_validate(
        {
            "id": "t",
            "name": "t",
            "nodes": [
                {
                    "id": "a",
                    "type": "core.prompt_input",
                    "params": {"text": "fast"},
                    "pause_on_complete": True,
                },
                {
                    "id": "b",
                    "type": "core.delay",
                    "params": {"seconds": 2.0, "value": "slow"},
                    "pause_on_complete": True,
                },
            ],
            "edges": [],
        }
    )
    run = await ctx.runner.start(definition)
    await wait_until_paused(ctx, run.id, ["a"])

    assert await ctx.runner.resume(run.id) is True

    # `b` is still running; when it lands it must hold the run on its own account.
    await wait_until_paused(ctx, run.id, ["b"])
    assert (await load_run(ctx, run.id)).status == RunStatus.PAUSED
    # And the first node is not dragged back into the second stop.
    assert (await node_runs(ctx, run.id))["a"].status == NodeStatus.SUCCESS

    assert await ctx.runner.resume(run.id) is True
    await wait_for(run.id)
    assert (await load_run(ctx, run.id)).status == RunStatus.SUCCESS


async def test_a_released_hold_is_never_waited_on_twice(ctx: AppContext) -> None:
    """The window `_hold`'s guard exists for, driven directly because it cannot be timed.

    `resume()` sets the event and the waiting node removes itself on a *later* turn of the
    loop. A node landing in between finds a record whose event is already set — and without
    the guard, waits on something that has already happened and walks straight past the
    pause it was configured for. Every path to that window goes through several awaits, so
    the losing order is rare rather than impossible, which is exactly the kind of bug a
    timing-based test claims to cover and does not.
    """
    runner = ctx.runner
    logged: list[str] = []

    async def log(line: str, stream: str = "stdout") -> None:
        logged.append(line)

    # The state `resume()` leaves behind for the turn before the waiter cleans up.
    spent = _Hold()
    spent.nodes.add("a")
    spent.event.set()
    runner._holds["run_probe"] = spent

    landed_late = asyncio.create_task(runner._hold("run_probe", "b", log))
    await asyncio.sleep(0.05)

    assert not landed_late.done(), "the late node walked past its own pause"
    assert runner.paused_nodes("run_probe") == ["b"]

    assert await runner.resume("run_probe") is True
    await landed_late
    # And the record is gone: the waiter owns its lifetime, and the stale one it replaced
    # must not take the live one down with it.
    assert runner.paused_nodes("run_probe") == []
    assert "run_probe" not in runner._holds


# ------------------------------------------------------------------ over HTTP


def test_the_resume_endpoint_refuses_a_run_that_is_not_waiting(client) -> None:
    response = client.post("/api/runs/run_nothing/resume")
    assert response.status_code == 409
    assert "not paused" in response.json()["detail"]


def test_a_run_payload_names_the_nodes_holding_it(client) -> None:
    """Live state, so it comes off the runner — this pins that the field is actually served,
    since a client that opens mid-pause has no event to learn it from."""
    created = client.post(
        "/api/pipelines",
        json={
            "id": "paused-probe",
            "name": "paused probe",
            "nodes": [
                {"id": "n1", "type": "core.prompt_input", "params": {"text": "hi"}},
            ],
            "edges": [],
        },
    )
    assert created.status_code == 201

    started = client.post("/api/pipelines/paused-probe/run", json={"use_cache": False})
    assert started.status_code == 202
    payload = client.get(f"/api/runs/{started.json()['run_id']}").json()
    assert payload["paused_nodes"] == []

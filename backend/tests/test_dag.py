"""Graph analysis: levels, cycles, validation and partial-run selection (§8.2, §6.3)."""

from __future__ import annotations

import pytest

from voxarium_studio.engine import dag
from voxarium_studio.plugins.registry import Registry
from voxarium_studio.schemas.pipeline import PipelineDefinition


def make(nodes: list[tuple[str, str]], edges: list[tuple[str, str, str, str]]) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        {
            "id": "t",
            "name": "t",
            "nodes": [{"id": node_id, "type": type_id} for node_id, type_id in nodes],
            "edges": [
                {"from": {"node": a, "port": ap}, "to": {"node": b, "port": bp}}
                for a, ap, b, bp in edges
            ],
        }
    )


CHAIN = make(
    [("a", "core.prompt_input"), ("b", "core.echo"), ("c", "core.echo")],
    [("a", "text", "b", "value"), ("b", "value", "c", "value")],
)

DIAMOND = make(
    [("a", "core.prompt_input"), ("b", "core.echo"), ("c", "core.echo"), ("d", "core.template")],
    [
        ("a", "text", "b", "value"),
        ("a", "text", "c", "value"),
        ("b", "value", "d", "a"),
        ("c", "value", "d", "b"),
    ],
)


def test_topological_levels_group_independent_nodes() -> None:
    assert dag.topological_levels(CHAIN) == [["a"], ["b"], ["c"]]
    # b and c only depend on a, so they share a level and run concurrently.
    assert dag.topological_levels(DIAMOND) == [["a"], ["b", "c"], ["d"]]


def test_cycles_are_rejected() -> None:
    cyclic = make(
        [("a", "core.echo"), ("b", "core.echo")],
        [("a", "value", "b", "value"), ("b", "value", "a", "value")],
    )
    with pytest.raises(dag.CycleError):
        dag.topological_levels(cyclic)


def test_self_edges_are_ignored_rather_than_deadlocking() -> None:
    looped = make([("a", "core.echo")], [("a", "value", "a", "value")])
    assert dag.topological_levels(looped) == [["a"]]


def test_ancestors_and_descendants() -> None:
    assert dag.ancestors(DIAMOND, "d") == {"a", "b", "c"}
    assert dag.descendants(DIAMOND, "a") == {"b", "c", "d"}
    assert dag.ancestors(DIAMOND, "a") == set()


def test_partial_run_selects_the_node_its_ancestors_and_everything_downstream() -> None:
    """Ancestors are included so they can resolve from cache instead of being skipped (§6.3)."""
    assert dag.selection_for_start_node(DIAMOND, "b") == {"a", "b", "d"}
    assert dag.selection_for_start_node(DIAMOND, "a") == {"a", "b", "c", "d"}
    with pytest.raises(KeyError):
        dag.selection_for_start_node(DIAMOND, "ghost")


def test_dangling_edges_are_refused_at_parse_time() -> None:
    """Rejecting here is what lets the graph helpers index nodes without guarding."""
    with pytest.raises(ValueError, match="unknown source node"):
        make([("a", "core.echo")], [("ghost", "value", "a", "value")])


def test_duplicate_node_ids_are_refused() -> None:
    with pytest.raises(ValueError, match="Duplicate node ids"):
        make([("a", "core.echo"), ("a", "core.echo")], [])


def test_validate_flags_unknown_node_types(registry: Registry) -> None:
    pipeline = make([("a", "nope.missing")], [])
    messages = [i.message for i in dag.validate(pipeline, registry) if i.level == "error"]
    assert any("Unknown node type" in message for message in messages)


def test_validate_flags_unknown_ports(registry: Registry) -> None:
    pipeline = make(
        [("a", "core.prompt_input"), ("b", "core.echo")], [("a", "ghost", "b", "value")]
    )
    messages = [i.message for i in dag.validate(pipeline, registry) if i.level == "error"]
    assert any("no output port 'ghost'" in message for message in messages)


def test_validate_flags_an_unsatisfied_required_input(registry: Registry) -> None:
    pipeline = make([("a", "core.echo")], [])  # `value` is required, unconnected, unset
    messages = [i.message for i in dag.validate(pipeline, registry) if i.level == "error"]
    assert any("neither\nconnected nor configured" in message.replace(" ", "\n", 1) or
               "is neither connected nor configured" in message for message in messages)


def test_validate_accepts_a_required_input_supplied_as_a_param(registry: Registry) -> None:
    pipeline = PipelineDefinition.model_validate(
        {"id": "t", "name": "t", "nodes": [{"id": "a", "type": "core.echo",
                                            "params": {"value": "set"}}], "edges": []}
    )
    assert [i for i in dag.validate(pipeline, registry) if i.level == "error"] == []


def test_validate_skips_disabled_nodes(registry: Registry) -> None:
    pipeline = PipelineDefinition.model_validate(
        {"id": "t", "name": "t",
         "nodes": [{"id": "a", "type": "core.echo", "disabled": True}], "edges": []}
    )
    assert [i for i in dag.validate(pipeline, registry) if i.level == "error"] == []

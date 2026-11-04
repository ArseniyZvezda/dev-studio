"""Graph analysis for pipelines: validation, topological levels, partial-run selection."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import combinations

from ..plugins.registry import Registry
from ..schemas.pipeline import PipelineDefinition


class CycleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GraphIssue:
    level: str
    """``error`` blocks execution; ``warning`` is surfaced but still runs."""

    message: str
    node_id: str | None = None


def adjacency(pipeline: PipelineDefinition) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (successors, predecessors) keyed by node id."""
    successors: dict[str, set[str]] = {node.id: set() for node in pipeline.nodes}
    predecessors: dict[str, set[str]] = {node.id: set() for node in pipeline.nodes}
    for edge in pipeline.edges:
        if edge.from_.node == edge.to.node:
            continue
        successors[edge.from_.node].add(edge.to.node)
        predecessors[edge.to.node].add(edge.from_.node)
    return successors, predecessors


def topological_levels(pipeline: PipelineDefinition) -> list[list[str]]:
    """Group nodes into levels; every node in a level can run concurrently (§8.2)."""
    successors, predecessors = adjacency(pipeline)
    indegree = {node_id: len(preds) for node_id, preds in predecessors.items()}
    ready = deque(sorted(n for n, d in indegree.items() if d == 0))
    levels: list[list[str]] = []
    visited = 0

    while ready:
        level = sorted(ready)
        ready.clear()
        levels.append(level)
        for node_id in level:
            visited += 1
            for successor in sorted(successors[node_id]):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)

    if visited != len(pipeline.nodes):
        remaining = sorted(n for n, d in indegree.items() if d > 0)
        raise CycleError(f"Pipeline contains a cycle involving: {', '.join(remaining)}")
    return levels


def ancestors(pipeline: PipelineDefinition, node_id: str) -> set[str]:
    _, predecessors = adjacency(pipeline)
    seen: set[str] = set()
    stack = list(predecessors.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(predecessors.get(current, ()))
    return seen


def descendants(pipeline: PipelineDefinition, node_id: str) -> set[str]:
    successors, _ = adjacency(pipeline)
    seen: set[str] = set()
    stack = list(successors.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(successors.get(current, ()))
    return seen


def selection_for_start_node(pipeline: PipelineDefinition, node_id: str) -> set[str]:
    """Nodes to execute when running *from* one node.

    Includes the node, everything downstream of it, and its ancestor closure — the
    ancestors are still "executed" but normally resolve straight from the node cache,
    which is what makes re-running a single node cheap (§6.3/§8.2).
    """
    if pipeline.node(node_id) is None:
        raise KeyError(f"Unknown node {node_id!r}")
    return {node_id} | descendants(pipeline, node_id) | ancestors(pipeline, node_id)


def loop_bodies(
    pipeline: PipelineDefinition, registry: Registry, scope: set[str]
) -> dict[str, set[str]]:
    """For every iterating node in ``scope``, the nodes that repeat with it.

    A loop's body is simply *everything to its right*: the node's descendant closure, cut
    to the scope being walked. There is no second kind of edge and no "end loop" node to
    pair it with — the graph already says which nodes depend on the value that repeats,
    and those are exactly the ones that have to run again for the next one (§8.2).

    Nested loops need no special case: an inner iterator is one of the outer body's nodes,
    so the outer walk hands it over as a unit and the recursion finds it again with a
    smaller scope.
    """
    bodies: dict[str, set[str]] = {}
    for node_id in sorted(scope):
        node = pipeline.node(node_id)
        if node is None or node.disabled:
            continue
        schema = registry.node_type(node.type)
        if schema is None or not schema.iterates:
            continue
        body = descendants(pipeline, node_id) & scope
        if body:
            bodies[node_id] = body
    return bodies


def validate(pipeline: PipelineDefinition, registry: Registry) -> list[GraphIssue]:
    """Check the graph against the registry before running it."""
    issues: list[GraphIssue] = []

    try:
        topological_levels(pipeline)
    except CycleError as exc:
        issues.append(GraphIssue("error", str(exc)))

    connected: dict[str, set[str]] = defaultdict(set)
    for edge in pipeline.edges:
        connected[edge.to.node].add(edge.to.port)

    for node in pipeline.nodes:
        schema = registry.node_type(node.type)
        if schema is None:
            issues.append(
                GraphIssue(
                    "error",
                    f"Unknown node type {node.type!r} — is its plugin installed and enabled?",
                    node.id,
                )
            )
            continue
        if node.disabled:
            continue
        for port in schema.inputs:
            if not port.required:
                continue
            has_edge = port.id in connected.get(node.id, set())
            has_param = node.params.get(port.id) not in (None, "")
            has_default = port.default is not None
            if not (has_edge or has_param or has_default):
                issues.append(
                    GraphIssue(
                        "error",
                        f"Required input {port.id!r} of node {node.id!r} is neither "
                        "connected nor configured",
                        node.id,
                    )
                )

    known_nodes = {node.id: node for node in pipeline.nodes}
    for edge in pipeline.edges:
        source_node = known_nodes.get(edge.from_.node)
        target_node = known_nodes.get(edge.to.node)
        if source_node is None or target_node is None:
            continue
        source_schema = registry.node_type(source_node.type)
        target_schema = registry.node_type(target_node.type)
        if source_schema and source_schema.output(edge.from_.port) is None:
            issues.append(
                GraphIssue(
                    "error",
                    f"Node {source_node.id!r} ({source_node.type}) has no output "
                    f"port {edge.from_.port!r}",
                    source_node.id,
                )
            )
        if target_schema and target_schema.input(edge.to.port) is None:
            issues.append(
                GraphIssue(
                    "error",
                    f"Node {target_node.id!r} ({target_node.type}) has no input "
                    f"port {edge.to.port!r}",
                    target_node.id,
                )
            )

    issues.extend(_validate_loops(pipeline, registry))
    return issues


def _validate_loops(
    pipeline: PipelineDefinition, registry: Registry
) -> list[GraphIssue]:
    """Two loops may overlap only by nesting, never by merely crossing.

    A node downstream of two iterators that are not one inside the other belongs to both
    bodies, and there is no answer to how often it should run: once per outer item, once
    per inner one, or once per pair. Nesting has an answer — the inner loop is part of the
    outer body and runs whole, every time — and it is the shape written by putting one
    loop after another, so it is the one that is allowed.
    """
    bodies = loop_bodies(pipeline, registry, {node.id for node in pipeline.nodes})
    issues: list[GraphIssue] = []
    for (first, first_body), (second, second_body) in combinations(bodies.items(), 2):
        shared = first_body & second_body
        if not shared or second in first_body or first in second_body:
            continue
        issues.append(
            GraphIssue(
                "error",
                f"Node(s) {', '.join(sorted(shared))} are downstream of both {first!r} "
                f"and {second!r}, which repeat independently — how often they should run "
                "has no answer. Put one loop inside the other, or split the branch.",
                first,
            )
        )
    return issues

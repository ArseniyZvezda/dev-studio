"""Pipeline definitions — YAML on disk, git-friendly and hand-editable (§5, §15.1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from .nodes import PortSchema


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeInstance(BaseModel):
    id: str
    type: str
    """References ``NodeTypeSchema.id``."""

    label: str | None = None
    position: Position = Field(default_factory=Position)
    params: dict[str, Any] = Field(default_factory=dict)
    """Literal values for inputs that are not fed by an edge."""

    pins: dict[str, bool] = Field(default_factory=dict)
    """Which inputs show a handle on *this* node, overriding ``PortSchema.connectable``.

    An override map rather than a list of pinned ports, because both answers have to be
    sayable: absent means "whatever the node type says", which is what keeps a pipeline
    following a plugin that later changes its mind about a port, while ``false`` is the
    user folding away a pin the type declares. Only divergences are written, so the YAML
    stays quiet for the ordinary node.
    """

    pause_on_complete: bool = False
    """Hold the run here once this node succeeds, until the user resumes it.

    The point is iterating over *part* of a pipeline: stop where the result is worth
    looking at, inspect it, and either continue or re-run from an earlier node (§8.2).
    """

    disabled: bool = False

    def pin_enabled(self, port: PortSchema) -> bool:
        """Whether ``port`` is a pin on this node — the instance's answer, or the type's."""
        value = self.pins.get(port.id)
        return port.connectable if value is None else bool(value)


class EdgeEndpoint(BaseModel):
    node: str
    port: str


class Edge(BaseModel):
    id: str | None = None
    from_: EdgeEndpoint = Field(alias="from")
    to: EdgeEndpoint

    model_config = {"populate_by_name": True}

    def model_post_init(self, _context: Any) -> None:
        if self.id is None:
            self.id = f"{self.from_.node}:{self.from_.port}->{self.to.node}:{self.to.port}"

    @property
    def source(self) -> EdgeEndpoint:
        return self.from_

    @property
    def target(self) -> EdgeEndpoint:
        return self.to


class PipelineDefinition(BaseModel):
    id: str
    name: str
    description: str = ""
    version: str = "1.0.0"
    nodes: list[NodeInstance] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    viewport: dict[str, float] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _validate_graph(self) -> "PipelineDefinition":
        ids = [node.id for node in self.nodes]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"Duplicate node ids: {sorted(duplicates)}")
        known = set(ids)
        for edge in self.edges:
            if edge.from_.node not in known:
                raise ValueError(f"Edge references unknown source node {edge.from_.node!r}")
            if edge.to.node not in known:
                raise ValueError(f"Edge references unknown target node {edge.to.node!r}")
        return self

    def node(self, node_id: str) -> NodeInstance | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.to.node == node_id]

    def outgoing(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.from_.node == node_id]

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialise with ``from``/``to`` edge keys as written in the spec's examples."""
        payload = self.model_dump(mode="json", by_alias=True, exclude_none=True)
        for edge in payload.get("edges", []):
            edge.pop("id", None)
        for node in payload.get("nodes", []):
            # Both default to "nothing to say", and a file the user is meant to read in a
            # diff should not carry `pins: {}` on every node to say it.
            if not node.get("pins"):
                node.pop("pins", None)
            if not node.get("pause_on_complete"):
                node.pop("pause_on_complete", None)
        return payload

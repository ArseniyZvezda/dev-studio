"""Node type schemas (§8.1).

These are the public contract plugins declare against, and the same objects the
frontend consumes to render both the palette and the generic node form (§9.3).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

PortType = Literal[
    "string",
    "text",
    "number",
    "integer",
    "boolean",
    "file",
    "directory",
    "path",
    "enum",
    "json",
    "color",
]
"""The spec's five core types plus ``text`` (multiline), ``integer``, ``directory``,
``path``, ``json`` and ``color`` — each maps to a distinct widget in the generic renderer.

``path`` is ``file`` or ``directory`` with the choice left open: it exists because
``core.path`` lets the user point at either, and the picker has to allow both rather than
make the node guess which one the user meant.

``color`` is a ``#rrggbb`` string with a swatch and an eyedropper in front of it. It is a
type rather than a convention because the control is what makes it usable: a node keying a
background against a colour typed as six hex digits is a node nobody can aim."""

ResourceClass = Literal["cpu", "gpu", "io", "agent"]
ExecutionKind = Literal["python_script", "python_function", "http_api"]


class PortSchema(BaseModel):
    """One input or output port. Inputs double as config fields when unconnected."""

    id: str
    label: str = ""
    type: PortType = "string"
    required: bool = True
    default: Any | None = None
    description: str = ""

    options: list[str] = Field(default_factory=list)
    """Allowed values when ``type == "enum"``."""

    accept: list[str] = Field(default_factory=list)
    """File extension hints for ``file``/``path`` ports, e.g. ``[".stl", ".obj"]``."""

    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None

    connectable: bool = True
    """Whether this port shows a Vue Flow handle *by default*.

    Only the default: ``NodeInstance.pins`` overrides it per node on the canvas, so a
    field the type author considered config can still be driven by an upstream node, and
    a pin nobody uses can be folded away. What the flag still decides is which of the two
    a node starts out as — declare it False for anything that is config in the ordinary
    case, exactly as before."""

    def model_post_init(self, _context: Any) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.id.replace("_", " ").title())

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"Port id must be alphanumeric/underscore, got {value!r}")
        return value


class ExecutionSpec(BaseModel):
    """How the engine actually runs the node.

    ``http_api`` is a first-class kind because many text-to-3D "models" are in
    practice a call to a hosted API rather than local inference (§8.1).
    """

    kind: ExecutionKind = "python_script"
    entry: str
    """Script path (relative to the plugin root), dotted function path, or URL template."""

    env: str | None = None
    """Name of the isolated plugin venv to run in; ``None`` uses the Studio interpreter."""

    resource_class: ResourceClass = "cpu"
    timeout_seconds: float | None = None
    method: str = "POST"
    """HTTP verb for ``http_api``."""

    headers: dict[str, str] = Field(default_factory=dict)
    cacheable: bool = True
    """Set False for nodes with side effects that must run every time."""


class NodeTypeSchema(BaseModel):
    """A node type as registered by a plugin or shipped built in."""

    id: str
    """Namespaced by plugin, e.g. ``hunyuan3d.image_to_mesh``."""

    name: str
    category: str = "General"
    """Drives palette grouping and the per-category accent colour (§7)."""

    version: str = "1.0.0"
    """Participates in the cache key so bumping it invalidates stale results (§8.2)."""

    description: str = ""
    color: str | None = None
    icon: str | None = None
    inputs: list[PortSchema] = Field(default_factory=list)
    outputs: list[PortSchema] = Field(default_factory=list)
    execution: ExecutionSpec
    plugin_id: str = "core"
    ui_component: str | None = None
    """Set by the loader when the plugin ships a custom Vue node component (§9.3)."""

    iterates: str | None = None
    """The output port this node **fans out** over: everything downstream of it on the
    canvas runs once per item (§8.2).

    The node's ``run()`` answers with a *list* on that port; the engine then walks the
    node's descendants once for each element, handing them the element alone. Its other
    outputs are constant across the iterations, and what is stored on the ``NodeRun`` is
    the whole list — that is what the node actually produced.

    A port rather than a boolean because the node still has ordinary outputs beside it,
    and because "which value repeats" is the only thing the engine needs to know to run a
    loop it otherwise treats as plain topological order. A type declaring it must also
    declare ``cacheable=False``: a cache hit answers with the list and would skip the body
    entirely, which is not the same run."""

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if "." not in value:
            raise ValueError(
                f"Node type id must be namespaced as '<plugin>.<name>', got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _validate_iterates(self) -> "NodeTypeSchema":
        if self.iterates is None:
            return self
        if self.output(self.iterates) is None:
            raise ValueError(
                f"Node type {self.id!r} iterates over {self.iterates!r}, "
                "which is not one of its output ports"
            )
        if self.execution.cacheable:
            raise ValueError(
                f"Node type {self.id!r} iterates and must declare cacheable=False — "
                "a cache hit would answer with the list and never run the loop body"
            )
        return self

    def input(self, port_id: str) -> PortSchema | None:
        return next((p for p in self.inputs if p.id == port_id), None)

    def output(self, port_id: str) -> PortSchema | None:
        return next((p for p in self.outputs if p.id == port_id), None)

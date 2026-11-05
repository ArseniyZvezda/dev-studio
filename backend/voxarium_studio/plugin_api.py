"""Stable import surface for plugins.

    from voxarium_studio.plugin_api import PluginRegistry, BaseNode, NodeTypeSchema

Everything re-exported here is considered public and versioned; anything else under
``voxarium_studio.*`` is internal and may change between releases.
"""

from .plugins.api import (
    ActionHandler,
    AutomationActionHandler,
    AutomationCancelled,
    BaseNode,
    Event,
    EventHook,
    NodeContext,
    PluginHost,
    PluginRegistry,
    ProjectOpenedHook,
    SearchHandler,
    SearchQuery,
    SearchResponse,
    SearchResult,
)
from .schemas.nodes import ExecutionSpec, NodeTypeSchema, PortSchema, PortType, ResourceClass
from .ws.events import EventType

__all__ = [
    "ActionHandler",
    "AutomationActionHandler",
    "AutomationCancelled",
    "BaseNode",
    "Event",
    "EventHook",
    "EventType",
    "ExecutionSpec",
    "NodeContext",
    "NodeTypeSchema",
    "PluginHost",
    "PluginRegistry",
    "PortSchema",
    "PortType",
    "ProjectOpenedHook",
    "ResourceClass",
    "SearchHandler",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
]

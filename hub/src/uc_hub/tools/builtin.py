"""The hub's tool catalogue (docs/ai-harness/DESIGN.md section 7), shared by
the agent loop and the MCP server."""

from __future__ import annotations

from . import building, live, manifest, reports, session
from .registry import ToolRegistry


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (building, manifest, live, session, reports):
        for tool in module.TOOLS:
            registry.register(tool)
    return registry

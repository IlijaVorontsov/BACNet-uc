"""Agent tools: the registry contract, the built-in catalogue and the runner
that validates, gates, executes and audits calls."""

from .builtin import build_registry
from .registry import ApprovalInfo, Tool, ToolCallContext, ToolRegistry, ToolResult
from .runner import Prepared, ToolRunner

__all__ = [
    "ApprovalInfo",
    "Prepared",
    "Tool",
    "ToolCallContext",
    "ToolRegistry",
    "ToolResult",
    "ToolRunner",
    "build_registry",
]

"""LedgerLens Agent subsystem package.

Exports the tool registry and secure tool execution interface.
"""
from agent.registry import (
    Tool,
    ToolRegistry,
    default_registry,
    execute_tool,
    validate_tool_arguments,
)
from agent.tools import (
    READ_ONLY_TOOLS,
    register_read_only_tools,
)

__all__ = [
    "Tool",
    "ToolRegistry",
    "default_registry",
    "execute_tool",
    "validate_tool_arguments",
    "READ_ONLY_TOOLS",
    "register_read_only_tools",
]

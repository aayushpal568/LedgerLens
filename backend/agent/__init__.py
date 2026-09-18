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

from agent.loop import (
    MAX_CLAUDE_TURNS,
    MAX_TOOL_CALLS,
    run_agent_loop,
    start_agent_run_background,
    set_global_llm_provider,
    get_llm_provider,
    request_run_cancellation,
)

__all__ = [
    "Tool",
    "ToolRegistry",
    "default_registry",
    "execute_tool",
    "validate_tool_arguments",
    "READ_ONLY_TOOLS",
    "register_read_only_tools",
    "MAX_CLAUDE_TURNS",
    "MAX_TOOL_CALLS",
    "run_agent_loop",
    "start_agent_run_background",
    "set_global_llm_provider",
    "get_llm_provider",
    "request_run_cancellation",
]

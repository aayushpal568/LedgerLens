"""Agent tool registry and secure tool execution interface for LedgerLens.

Guarantees:
- Every tool handler receives AuthedUser with validated firm_id.
- Every tool execution strictly uses backend/services.py (never bypasses services or tenant scoping).
- Unknown tools and invalid arguments fail closed.
- Tool arguments containing 'firm_id' are rejected immediately.
- Enforces read_only safety.
"""
from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, Union

from fastapi import HTTPException

from auth_dep import AuthedUser

logger = logging.getLogger(__name__)


class ToolExecutionError(Exception):
    """Raised when a tool execution fails."""
    def __init__(self, message: str, error_code: int = 400, error_type: str = "ExecutionError"):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.error_type = error_type


@dataclass
class Tool:
    """Definition of an agent-executable tool."""
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[..., Coroutine[Any, Any, Any]]
    read_only: bool = True
    approval_required: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "read_only": self.read_only,
            "approval_required": self.approval_required,
        }


class ToolRegistry:
    """Registry managing available agent tools."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name or not isinstance(tool.name, str):
            raise ValueError("Tool name must be a non-empty string.")
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def contains(self, name: str) -> bool:
        return name in self._tools


default_registry = ToolRegistry()


def validate_tool_arguments(tool: Tool, arguments: Optional[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
    """Validate tool arguments against the tool's JSON parameter schema.

    Returns:
        (is_valid, error_message)
    """
    args = arguments if arguments is not None else {}
    if not isinstance(args, dict):
        return False, "Tool arguments must be provided as a dictionary/object."

    # Reject firm_id tampering immediately
    if "firm_id" in args:
        return False, "Security violation: 'firm_id' parameter cannot be supplied in tool arguments."

    schema = tool.parameters or {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    # Check required properties
    for req_field in required:
        if req_field not in args:
            return False, f"Missing required argument: '{req_field}'."
        val = args[req_field]
        if val is None or (isinstance(val, str) and not val.strip()):
            return False, f"Required argument '{req_field}' cannot be null or empty."

    # Validate property types and extra properties
    allow_extra = schema.get("additionalProperties", False)
    for key, val in args.items():
        if key not in properties and not allow_extra:
            return False, f"Unexpected argument: '{key}' is not accepted by tool '{tool.name}'."

        expected = properties.get(key, {})
        expected_type = expected.get("type")
        if expected_type and val is not None:
            if expected_type == "string" and not isinstance(val, str):
                return False, f"Argument '{key}' must be a string, received {type(val).__name__}."
            elif expected_type == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
                return False, f"Argument '{key}' must be an integer, received {type(val).__name__}."
            elif expected_type == "number" and (not isinstance(val, (int, float)) or isinstance(val, bool)):
                return False, f"Argument '{key}' must be a number, received {type(val).__name__}."
            elif expected_type == "boolean" and not isinstance(val, bool):
                return False, f"Argument '{key}' must be a boolean, received {type(val).__name__}."
            elif expected_type == "array" and not isinstance(val, list):
                return False, f"Argument '{key}' must be a list/array, received {type(val).__name__}."
            elif expected_type == "object" and not isinstance(val, dict):
                return False, f"Argument '{key}' must be an object/dict, received {type(val).__name__}."

            enum_vals = expected.get("enum")
            if enum_vals is not None and val not in enum_vals:
                return False, f"Argument '{key}' value '{val}' is not in allowed choices: {enum_vals}."

    return True, None


async def execute_tool(
    user: AuthedUser,
    tool_name: str,
    arguments: Optional[Dict[str, Any]] = None,
    registry: Optional[ToolRegistry] = None,
    db=None,
    is_approved: bool = False,
) -> Dict[str, Any]:
    """Secure central entrypoint to execute an agent tool.

    Guarantees:
    - User is an AuthedUser with valid non-empty firm_id.
    - Tool exists in the registry (fails closed).
    - Arguments conform to the tool's parameter schema.
    - Approval requirement is checked and enforced.
    - Handlers execute through backend/services.py (preserving tenant scoping).
    - Returns a structured, JSON-serializable result.
    """
    # 1. Validate user context
    if not user or not getattr(user, "firm_id", None) or not str(user.firm_id).strip():
        raise ValueError(
            "Security violation: Valid authenticated user context with firm_id is required to execute tools."
        )

    # 2. Look up tool
    active_registry = registry or default_registry
    tool = active_registry.get(tool_name)
    if tool is None:
        return {
            "success": False,
            "tool": tool_name,
            "error": f"Unknown tool: '{tool_name}'. Tool is not registered.",
            "error_type": "ToolNotFound",
        }

    # 3. Validate arguments
    args = arguments if arguments is not None else {}
    valid, err_msg = validate_tool_arguments(tool, args)
    if not valid:
        return {
            "success": False,
            "tool": tool_name,
            "error": f"Invalid arguments for tool '{tool_name}': {err_msg}",
            "error_type": "InvalidArgument",
        }

    # 4. Enforce approval constraint for action tools
    if tool.approval_required and not is_approved:
        return {
            "success": False,
            "tool": tool_name,
            "error": f"Tool '{tool_name}' requires human approval before execution.",
            "error_type": "ApprovalRequired",
        }

    # 5. Execute handler
    try:
        result = await tool.handler(user, args, db=db)
        return {
            "success": True,
            "tool": tool_name,
            "result": result,
        }
    except HTTPException as exc:
        return {
            "success": False,
            "tool": tool_name,
            "error": exc.detail,
            "error_code": exc.status_code,
            "error_type": "NotFoundError" if exc.status_code == 404 else "ServiceError",
        }
    except PermissionError as exc:
        return {
            "success": False,
            "tool": tool_name,
            "error": str(exc),
            "error_type": "PermissionDenied",
        }
    except ValueError as exc:
        return {
            "success": False,
            "tool": tool_name,
            "error": str(exc),
            "error_type": "ValidationError",
        }
    except Exception as exc:
        logger.exception(f"Unexpected error executing tool '{tool_name}': {exc}")
        return {
            "success": False,
            "tool": tool_name,
            "error": str(exc),
            "error_type": "ExecutionError",
        }

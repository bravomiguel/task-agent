"""Tool description middleware for adding description field to tools."""

from __future__ import annotations

from typing import Callable, Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.tools import BaseTool


class ToolDescriptionMiddleware(AgentMiddleware):
    """Middleware that wraps all tools to add a 'description' field.

    This allows the LLM to explain what it's doing when calling a tool,
    which is then surfaced in the frontend UI.

    Uses ToolWithDescription wrapper to dynamically extend tool schemas
    without serialization (avoids PydanticInvalidForJsonSchema errors).
    """

    def __init__(self):
        super().__init__()
        # Cache wrapped tools to avoid re-wrapping on every call
        self._wrapped_tools_cache: dict[str, BaseTool] = {}

    def _wrap_tool(self, tool: BaseTool) -> BaseTool:
        """Wrap a single tool with description field."""
        # Import here to avoid circular imports
        from agent.tool_wrapper import ToolWithDescription

        # Check cache first
        tool_id = id(tool)
        cache_key = f"{tool.name}_{tool_id}"
        if cache_key in self._wrapped_tools_cache:
            return self._wrapped_tools_cache[cache_key]

        # Only wrap BaseTool objects with args_schema
        if hasattr(tool, "args_schema") and tool.args_schema is not None:
            wrapped = ToolWithDescription(tool)
            self._wrapped_tools_cache[cache_key] = wrapped
            return wrapped

        return tool

    def _wrap_tools(self, tools: list) -> list:
        """Wrap all tools in the list."""
        wrapped = []
        for tool in tools:
            if isinstance(tool, BaseTool):
                wrapped.append(self._wrap_tool(tool))
            else:
                # Pass through dict-format tools unchanged
                wrapped.append(tool)
        return wrapped

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Wrap tools with description field before model call."""
        if request.tools:
            wrapped_tools = self._wrap_tools(request.tools)
            request = request.override(tools=wrapped_tools)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Wrap tools with description field."""
        if request.tools:
            wrapped_tools = self._wrap_tools(request.tools)
            request = request.override(tools=wrapped_tools)
        return await handler(request)

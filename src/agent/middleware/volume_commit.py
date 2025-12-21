"""Volume commit middleware for persisting file changes."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

import modal
from langchain.agents.middleware import AgentMiddleware

from agent.middleware.modal_sandbox import ModalSandboxState


class VolumeCommitMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that commits Modal volume after file write operations.

    Ensures files written by write_file, edit_file, or execute tools are
    immediately persisted to the Modal Volume before returning to the agent.
    """

    state_schema = ModalSandboxState

    def __init__(self, volume_name: str = "threads"):
        super().__init__()
        self._volume_name = volume_name

    def wrap_tool_call(
        self,
        request: Any,  # ToolCallRequest
        handler: Callable[[Any], Any],  # Callable[[ToolCallRequest], ToolCallResponse]
    ) -> Any:  # ToolCallResponse
        """Commit volume after file write operations."""
        # Execute the tool
        response = handler(request)

        # Get tool name and thread_id from request
        tool_name = request.tool.name if hasattr(request, 'tool') else None
        thread_id = request.state.get("thread_id") if hasattr(request, 'state') else None

        # Commit volume if tool is file-related
        if tool_name in ["write_file", "edit_file"]:
            try:
                volume = modal.Volume.from_name(self._volume_name, version=2)
                volume.commit()
            except Exception as e:
                print(f"Warning: Failed to commit volume after {tool_name}: {e}")

        elif tool_name == "execute" and thread_id:
            # Check if command involves files in this thread's folder
            tool_input = request.tool_input if hasattr(request, 'tool_input') else {}
            command = tool_input.get("command", "")

            if isinstance(command, str) and f"/threads/{thread_id}/" in command:
                try:
                    volume = modal.Volume.from_name(self._volume_name, version=2)
                    volume.commit()
                except Exception as e:
                    print(f"Warning: Failed to commit volume after execute: {e}")

        return response

    async def awrap_tool_call(
        self,
        request: Any,  # ToolCallRequest
        handler: Callable[[Any], Awaitable[Any]],  # Callable[[ToolCallRequest], Awaitable[ToolCallResponse]]
    ) -> Any:  # ToolCallResponse
        """Async version: Commit volume after file write operations."""
        # Execute the tool
        response = await handler(request)

        # Get tool name and thread_id from request
        tool_name = request.tool.name if hasattr(request, 'tool') else None
        thread_id = request.state.get("thread_id") if hasattr(request, 'state') else None

        # Commit volume if tool is file-related
        if tool_name in ["write_file", "edit_file"]:
            try:
                volume = modal.Volume.from_name(self._volume_name, version=2)
                volume.commit()
            except Exception as e:
                print(f"Warning: Failed to commit volume after {tool_name}: {e}")

        elif tool_name == "execute" and thread_id:
            # Check if command involves files in this thread's folder
            tool_input = request.tool_input if hasattr(request, 'tool_input') else {}
            command = tool_input.get("command", "")

            if isinstance(command, str) and f"/threads/{thread_id}/" in command:
                try:
                    volume = modal.Volume.from_name(self._volume_name, version=2)
                    volume.commit()
                except Exception as e:
                    print(f"Warning: Failed to commit volume after execute: {e}")

        return response

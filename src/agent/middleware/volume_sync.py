"""Volume sync middleware for persisting and refreshing file changes."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

import modal
from langchain.agents.middleware import AgentMiddleware

from agent.middleware.modal_sandbox import ModalSandboxState

# Tools that read from the filesystem
READ_TOOLS = {"ls", "read_file", "view_image", "glob", "grep"}

# Tools that write to the filesystem
WRITE_TOOLS = {"write_file", "edit_file"}


class VolumeSyncMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that syncs Modal volume before reads and after writes.

    - Reloads volume before read operations to see files from other processes
    - Commits volume after write operations to persist changes for other processes
    """

    state_schema = ModalSandboxState

    def __init__(self, volume_name: str = "threads"):
        super().__init__()
        self._volume_name = volume_name

    def _get_volume(self) -> modal.Volume:
        """Get the Modal volume instance."""
        return modal.Volume.from_name(self._volume_name, version=2)

    def wrap_tool_call(
        self,
        request: Any,  # ToolCallRequest
        handler: Callable[[Any], Any],  # Callable[[ToolCallRequest], ToolCallResponse]
    ) -> Any:  # ToolCallResponse
        """Sync volume before reads and after writes."""
        tool_name = request.tool.name if hasattr(request, 'tool') else None
        thread_id = request.state.get("thread_id") if hasattr(request, 'state') else None

        # PRE-TOOL: Reload before read operations
        if tool_name in READ_TOOLS:
            try:
                volume = self._get_volume()
                volume.reload()
            except Exception as e:
                print(f"Warning: Failed to reload volume before {tool_name}: {e}")

        # Execute the tool
        response = handler(request)

        # POST-TOOL: Commit after write operations
        if tool_name in WRITE_TOOLS:
            try:
                volume = self._get_volume()
                volume.commit()
            except Exception as e:
                print(f"Warning: Failed to commit volume after {tool_name}: {e}")

        elif tool_name == "execute" and thread_id:
            # Check if command involves files in this thread's folder
            tool_input = request.tool_input if hasattr(request, 'tool_input') else {}
            command = tool_input.get("command", "")

            if isinstance(command, str) and f"/threads/{thread_id}/" in command:
                try:
                    volume = self._get_volume()
                    volume.commit()
                except Exception as e:
                    print(f"Warning: Failed to commit volume after execute: {e}")

        return response

    async def awrap_tool_call(
        self,
        request: Any,  # ToolCallRequest
        handler: Callable[[Any], Awaitable[Any]],  # Callable[[ToolCallRequest], Awaitable[ToolCallResponse]]
    ) -> Any:  # ToolCallResponse
        """Async version: Sync volume before reads and after writes."""
        tool_name = request.tool.name if hasattr(request, 'tool') else None
        thread_id = request.state.get("thread_id") if hasattr(request, 'state') else None

        # PRE-TOOL: Reload before read operations
        if tool_name in READ_TOOLS:
            try:
                volume = self._get_volume()
                volume.reload()
            except Exception as e:
                print(f"Warning: Failed to reload volume before {tool_name}: {e}")

        # Execute the tool
        response = await handler(request)

        # POST-TOOL: Commit after write operations
        if tool_name in WRITE_TOOLS:
            try:
                volume = self._get_volume()
                volume.commit()
            except Exception as e:
                print(f"Warning: Failed to commit volume after {tool_name}: {e}")

        elif tool_name == "execute" and thread_id:
            # Check if command involves files in this thread's folder
            tool_input = request.tool_input if hasattr(request, 'tool_input') else {}
            command = tool_input.get("command", "")

            if isinstance(command, str) and f"/threads/{thread_id}/" in command:
                try:
                    volume = self._get_volume()
                    volume.commit()
                except Exception as e:
                    print(f"Warning: Failed to commit volume after execute: {e}")

        return response



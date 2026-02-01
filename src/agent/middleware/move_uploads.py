"""Middleware for moving temporary uploads to thread folder."""

from __future__ import annotations

from typing import Any

import modal
from langchain.agents.middleware import AgentMiddleware
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.runtime import Runtime

from agent.middleware.modal_sandbox import ModalSandboxState


class MoveUploadsMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that moves files from temp-uploads staging to thread uploads folder.

    When a user attaches files before a thread exists, they're uploaded to
    /threads/temp-uploads/{temp_id}/. This middleware moves those files to
    /threads/{thread_id}/uploads/ before the agent runs.
    """

    state_schema = ModalSandboxState

    def before_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Move temp uploads to thread folder if temp_uploads_id is in config."""

        # Get temp_uploads_id from config
        config = var_child_runnable_config.get()
        if not config:
            return None

        configurable = config.get("configurable", {})
        temp_uploads_id = configurable.get("temp_uploads_id")
        if not temp_uploads_id:
            return None  # No temp uploads to move

        thread_id = state.get("thread_id")
        sandbox_id = state.get("modal_sandbox_id")

        if not thread_id or not sandbox_id:
            print("Warning: Cannot move uploads - missing thread_id or sandbox_id")
            return None

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)

            # Check if temp uploads folder exists
            check_process = sandbox.exec(
                "ls", f"/threads/temp-uploads/{temp_uploads_id}",
                timeout=10
            )
            check_process.wait()

            if check_process.returncode != 0:
                # No temp uploads folder, nothing to move
                return None

            # Ensure destination uploads folder exists
            mkdir_process = sandbox.exec(
                "mkdir", "-p", f"/threads/{thread_id}/uploads",
                timeout=10
            )
            mkdir_process.wait()

            # Move all files from temp to thread uploads
            # Using mv with wildcard via shell
            move_process = sandbox.exec(
                "sh", "-c",
                f"mv /threads/temp-uploads/{temp_uploads_id}/* /threads/{thread_id}/uploads/",
                timeout=30
            )
            move_process.wait()

            if move_process.returncode == 0:
                # Clean up empty temp folder
                rmdir_process = sandbox.exec(
                    "rm", "-rf", f"/threads/temp-uploads/{temp_uploads_id}",
                    timeout=10
                )
                rmdir_process.wait()

                # Sync volume to persist changes for other processes
                sync_process = sandbox.exec("sync", "/threads", timeout=30)
                sync_process.wait()

                print(f"Moved uploads from temp-uploads/{temp_uploads_id} to {thread_id}/uploads/")
            else:
                stderr = move_process.stderr.read() if move_process.stderr else ""
                print(f"Warning: Failed to move temp uploads: {stderr}")

        except Exception as e:
            print(f"Error moving temp uploads: {e}")

        return None

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

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

    Files are uploaded to /threads/temp-uploads/{temp_id}/ before submission.
    This middleware moves only the files listed in attached_files to
    /threads/{thread_id}/uploads/, discarding any removed attachments.
    """

    state_schema = ModalSandboxState

    def before_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Move temp uploads to thread folder if temp_uploads_id is in config."""

        # Get temp_uploads_id and attached_files from config
        config = var_child_runnable_config.get()
        if not config:
            return None

        configurable = config.get("configurable", {})
        temp_uploads_id = configurable.get("temp_uploads_id")
        if not temp_uploads_id:
            return None  # No temp uploads to move

        # Get list of files that are actually attached (user didn't remove them)
        attached_files: list[str] = configurable.get("attached_files", [])

        thread_id = state.get("thread_id")
        sandbox_id = state.get("modal_sandbox_id")

        if not thread_id or not sandbox_id:
            print("Warning: Cannot move uploads - missing thread_id or sandbox_id")
            return None

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            temp_path = f"/threads/temp-uploads/{temp_uploads_id}"
            dest_path = f"/threads/{thread_id}/uploads"

            # Check if temp uploads folder exists
            check_process = sandbox.exec("ls", temp_path, timeout=10)
            check_process.wait()

            if check_process.returncode != 0:
                # No temp uploads folder, nothing to move
                return None

            # Ensure destination uploads folder exists
            mkdir_process = sandbox.exec("mkdir", "-p", dest_path, timeout=10)
            mkdir_process.wait()

            if attached_files:
                # Move only the files that are still attached
                for filename in attached_files:
                    src = f"{temp_path}/{filename}"
                    move_process = sandbox.exec(
                        "mv", src, f"{dest_path}/",
                        timeout=30
                    )
                    move_process.wait()
                    if move_process.returncode != 0:
                        stderr = move_process.stderr.read() if move_process.stderr else ""
                        print(f"Warning: Failed to move {filename}: {stderr}")

            # Clean up temp folder (including any files user removed)
            rmdir_process = sandbox.exec("rm", "-rf", temp_path, timeout=10)
            rmdir_process.wait()

            # Sync volume to persist changes for other processes
            sync_process = sandbox.exec("sync", "/threads", timeout=30)
            sync_process.wait()

            if attached_files:
                print(f"Moved {len(attached_files)} file(s) to {thread_id}/uploads/")

        except Exception as e:
            print(f"Error moving temp uploads: {e}")

        return None

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

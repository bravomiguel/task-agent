from __future__ import annotations

import asyncio
import json
import os
import requests
import shlex
import uuid
from typing import Annotated, Any, NotRequired
import time
from datetime import datetime, timezone
from langchain.agents.middleware import AgentMiddleware, AgentState
from typing import Callable, Awaitable
from langchain_core.messages import HumanMessage
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.runtime import Runtime
import modal


# Create Modal image with rclone and dependencies
rclone_image = (
    modal.Image.debian_slim()
    .apt_install("curl", "unzip", "jq", "ripgrep")
    .run_commands(
        # Install latest rclone binary
        "curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip",
        "unzip -q rclone-current-linux-amd64.zip",
        "cp rclone-*-linux-amd64/rclone /usr/local/bin/",
        "chmod 755 /usr/local/bin/rclone",
        "rm -rf rclone-*",
        # Verify installation
        "rclone version"
    )
)


def fetch_composio_gdrive_token() -> dict[str, str]:
    """Fetch Google Drive token from Composio API.

    Returns:
        Dict with RCLONE_CONFIG_* environment variables for Google Drive

    Raises:
        RuntimeError: If API call fails or token not found
    """
    composio_api_key = os.environ.get("COMPOSIO_API_KEY")
    if not composio_api_key:
        raise RuntimeError("COMPOSIO_API_KEY not found in environment")

    # Hardcoded connected account ID for now (will be dynamic later)
    connected_account_id = "ca_VHLP3Y6uKgAZ"

    url = f"https://backend.composio.dev/api/v3/connected_accounts/{connected_account_id}"
    headers = {"X-API-Key": composio_api_key}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Extract tokens from response
        access_token = data.get("data", {}).get("access_token")
        refresh_token = data.get("data", {}).get("refresh_token")

        if not access_token or not refresh_token:
            raise RuntimeError("access_token or refresh_token not found in Composio response")

        # Build rclone token JSON
        token_json = json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer"
        })

        # Return environment variables for rclone
        return {
            "RCLONE_CONFIG_GDRIVE_TYPE": "drive",
            "RCLONE_CONFIG_GDRIVE_SCOPE": "drive",
            "RCLONE_CONFIG_GDRIVE_TOKEN": token_json,
        }

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch Composio token: {e}")


class ModalSandboxState(AgentState):
    """Extended state schema with Modal sandbox ID and Google Drive access."""
    modal_sandbox_id: NotRequired[str]
    thread_id: NotRequired[str]
    modal_snapshot_id: NotRequired[str]
    gdrive_access_token: NotRequired[str]


class ModalSandboxMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that manages Modal sandbox lifecycle per thread.

    Creates a new Modal sandbox when a thread starts and relies on Modal's
    idle_timeout to automatically terminate inactive sandboxes.
    """

    state_schema = ModalSandboxState

    def __init__(
        self,
        workdir: str = "/workspace",
        startup_timeout: int = 180,
        idle_timeout: int = 60 * 3,  # 3 minutes
        max_timeout: int = 60 * 60 * 24,   # 24 hours
        volume_name: str = "threads",
        memory_volume_name: str = "memories",
    ):
        super().__init__()
        self._workdir = workdir
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._max_timeout = max_timeout
        self._volume_name = volume_name
        self._memory_volume_name = memory_volume_name

    def before_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Create or reconnect to Modal sandbox for this thread."""
        import modal

        existing_sandbox_id = state.get("modal_sandbox_id")

        if existing_sandbox_id:
            try:
                sandbox = modal.Sandbox.from_id(existing_sandbox_id)

                # Verify sandbox is alive
                try:
                    process = sandbox.exec("echo", "alive", timeout=5)
                    process.wait()
                    if process.returncode == 0:
                        return None  # Reuse existing
                except Exception:
                    pass
            except Exception:
                pass

        # Get thread_id from config, state, or generate new
        # Must determine thread_id BEFORE creating sandbox to set workdir
        thread_id = state.get("thread_id")
        if not thread_id:
            # Try to get from RunnableConfig context var
            config = var_child_runnable_config.get()
            if config:
                thread_id = config.get("configurable", {}).get("thread_id")
        if not thread_id and runtime.context and hasattr(runtime.context, 'thread_id'):
            thread_id = runtime.context.thread_id
        if not thread_id:
            thread_id = str(uuid.uuid4())

        # Get or create v2 volumes for persistent storage
        thread_volume = modal.Volume.from_name(
            self._volume_name,
            create_if_missing=True,
            version=2
        )
        memory_volume = modal.Volume.from_name(
            self._memory_volume_name,
            create_if_missing=True,
            version=2
        )

        # Check if we should restore from a snapshot
        snapshot_id = state.get("modal_snapshot_id")
        image = None
        if snapshot_id:
            try:
                image = modal.Image.from_id(snapshot_id)
            except Exception:
                # Snapshot not found or expired, proceed without it
                pass

        # Use rclone image if no snapshot to restore
        if image is None:
            image = rclone_image

        # Fetch Google Drive token from Composio
        gdrive_access_token = None
        try:
            gdrive_env = fetch_composio_gdrive_token()
            # Extract access token from the token JSON for API calls
            token_json_str = gdrive_env.get("RCLONE_CONFIG_GDRIVE_TOKEN", "{}")
            token_data = json.loads(token_json_str)
            gdrive_access_token = token_data.get("access_token")
        except Exception as e:
            # Log error but continue without Google Drive access
            print(f"Warning: Could not fetch Google Drive token: {e}")
            gdrive_env = {}

        # Create new sandbox with volumes mounted and workdir set to thread folder
        app = modal.App.lookup("agent-sandbox", create_if_missing=True)
        sandbox = modal.Sandbox.create(
            app=app,
            image=image,
            workdir=f"/threads/{thread_id}",
            timeout=self._max_timeout,
            idle_timeout=self._idle_timeout,
            volumes={
                "/threads": thread_volume,
                "/memories": memory_volume,
            },
            env=gdrive_env,  # Inject Google Drive rclone config
            verbose=True,
        )

        # Poll until ready
        for _ in range(self._startup_timeout // 2):
            if sandbox.poll() is not None:
                raise RuntimeError("Modal sandbox terminated during startup")
            try:
                process = sandbox.exec("echo", "ready", timeout=5)
                process.wait()
                if process.returncode == 0:
                    break
            except Exception:
                pass
            time.sleep(2)
        else:
            sandbox.terminate()
            raise RuntimeError(
                f"Modal sandbox failed to start within {self._startup_timeout}s")

        # Create thread subfolder if it doesn't exist (safety measure)
        # The volume is mounted at /threads, so mkdir ensures the subdirectory exists
        process = sandbox.exec(
            "mkdir", "-p", f"/threads/{thread_id}", timeout=10)
        process.wait()

        state_updates = {
            "modal_sandbox_id": sandbox.object_id,
            "thread_id": thread_id,
        }

        # Add access token to state if available
        if gdrive_access_token:
            state_updates["gdrive_access_token"] = gdrive_access_token

        return state_updates

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def after_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Create filesystem snapshot to preserve /workspace state."""
        import modal

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return None

        try:
            # Reconnect to sandbox
            sandbox = modal.Sandbox.from_id(sandbox_id)

            # Create filesystem snapshot
            snapshot = sandbox.snapshot_filesystem(timeout=55)

            return {
                "modal_snapshot_id": snapshot.object_id,
            }
        except Exception:
            # If snapshotting fails, continue without updating snapshot
            return None

    async def aafter_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.after_agent(state, runtime)


class DynamicContextMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that injects dynamic context (thread ID, access tokens) into system prompt."""

    state_schema = ModalSandboxState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject dynamic context into system prompt."""
        if not request.system_prompt:
            return handler(request)

        dynamic_context = ""

        # Add thread context
        thread_id = request.state.get("thread_id")
        if thread_id:
            dynamic_context += (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/`."
            )

        # Add Google Drive access token if available
        gdrive_token = request.state.get("gdrive_access_token")
        if gdrive_token:
            dynamic_context += (
                f"\n\n### Google Drive Access Token\n"
                f"For Google Drive API requests, use this access token:\n"
                f"```\n{gdrive_token}\n```"
            )

        if dynamic_context:
            request.system_prompt = request.system_prompt + dynamic_context

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject dynamic context into system prompt."""
        if not request.system_prompt:
            return await handler(request)

        dynamic_context = ""

        # Add thread context
        thread_id = request.state.get("thread_id")
        if thread_id:
            dynamic_context += (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/`."
            )

        # Add Google Drive access token if available
        gdrive_token = request.state.get("gdrive_access_token")
        if gdrive_token:
            dynamic_context += (
                f"\n\n### Google Drive Access Token\n"
                f"For Google Drive API requests, use this access token:\n"
                f"```\n{gdrive_token}\n```"
            )

        if dynamic_context:
            request.system_prompt = request.system_prompt + dynamic_context

        return await handler(request)


# Extend AgentState to include thread_title


class ThreadTitleState(AgentState):
    thread_title: NotRequired[str]


class ThreadTitleMiddleware(AgentMiddleware[ThreadTitleState]):
    """Middleware that generates a thread title from the initial user message."""

    state_schema = ThreadTitleState

    def __init__(self, llm):
        """Initialize with an LLM for title generation."""
        super().__init__()
        self.llm = llm

    def before_agent(
        self, state: ThreadTitleState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Sync version: Generate thread title on first message."""
        # Only run if thread_title doesn't exist (first message)
        if "thread_title" in state and state["thread_title"]:
            return None

        # Get the first user message
        messages = state["messages"]
        first_user_msg = next(
            (m for m in messages if isinstance(m, HumanMessage)), None)

        if not first_user_msg:
            return None

        # Use LLM to generate a short title
        title_prompt = f"Generate a short title for this conversation, do not include double quotes: {first_user_msg.content}"
        title = self.llm.invoke(title_prompt).content

        return {"thread_title": title}

    async def abefore_agent(
        self, state: ThreadTitleState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version: Generate thread title on first message."""
        # Only run if thread_title doesn't exist (first message)
        if "thread_title" in state and state["thread_title"]:
            return None

        # Get the first user message
        messages = state["messages"]
        first_user_msg = next(
            (m for m in messages if isinstance(m, HumanMessage)), None)

        if not first_user_msg:
            return None

        # Use LLM to generate a short title (async)
        title_prompt = f"Generate a short title for this conversation, do not include double quotes: {first_user_msg.content}"
        title = (await self.llm.ainvoke(title_prompt)).content

        return {"thread_title": title}


def is_done_reducer(left, right):
    # If both are None, default to False
    if left is None and right is None:
        return False
    # If only right is None, keep left
    if right is None:
        return left
    # Otherwise, use right (the new value)
    return right


class IsDoneState(AgentState):
    is_done: Annotated[NotRequired[bool], is_done_reducer]


class IsDoneMiddleware(AgentMiddleware[IsDoneState]):
    """Middleware that adds is_done boolean to agent state."""
    state_schema = IsDoneState


class ReviewState(AgentState):
    review_message: NotRequired[str]


class ReviewMessageMiddleware(AgentMiddleware[ReviewState]):
    state_schema = ReviewState

    def __init__(self, llm):
        """Initialize with an LLM for title generation."""
        super().__init__()
        self.llm = llm

    def after_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:
        """Sync version: Generate review message after agent completes."""
        messages = state["messages"]

        if not messages:
            return {"review_message": "No action needed."}

        # Get last 5 messages for context
        recent_messages = messages[-5:]
        messages_text = "\n\n".join([
            f"{msg.__class__.__name__}: {msg.content}"
            for msg in recent_messages
        ])

        summary_prompt = f"""Based on this conversation, what does the user need to do next?

        Recent conversation:
        {messages_text}

        Provide a brief, imperative instruction addressed directly to the user (1 sentence).
        Include enough context to be descriptive but stay concise.
        Examples: "Review the Modal sandbox changes and test the agent." or "Run the updated migration script for the database." or "No action needed."
        Be direct and actionable."""

        response = self.llm.invoke(
            [{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}

    async def aafter_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:
        """Async version: Generate review message after agent completes."""
        messages = state["messages"]

        if not messages:
            return {"review_message": "No action needed."}

        # Get last 5 messages for context
        recent_messages = messages[-5:]
        messages_text = "\n\n".join([
            f"{msg.__class__.__name__}: {msg.content}"
            for msg in recent_messages
        ])

        summary_prompt = f"""Based on this conversation, what does the user need to do next?

        Recent conversation:
        {messages_text}

        Provide a brief, imperative instruction addressed directly to the user (1 sentence).
        Include enough context to be descriptive but stay concise.
        Examples: "Review the Modal sandbox changes and test the agent." or "Run the updated migration script for the database." or "No action needed."
        Be direct and actionable."""

        response = await self.llm.ainvoke([{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}


class DateTimeContextMiddleware(AgentMiddleware):
    """Middleware that injects current date/time into user messages."""

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Prepend timestamp to the last user message."""
        # Find the last HumanMessage and prepend timestamp
        for msg in reversed(request.messages):
            if isinstance(msg, HumanMessage):
                current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                # Only prepend if not already timestamped
                if not msg.content.startswith("["):
                    msg.content = f"[{current_time}] {msg.content}"
                break
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Prepend timestamp to the last user message."""
        # Find the last HumanMessage and prepend timestamp
        for msg in reversed(request.messages):
            if isinstance(msg, HumanMessage):
                current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                # Only prepend if not already timestamped
                if not msg.content.startswith("["):
                    msg.content = f"[{current_time}] {msg.content}"
                break
        return await handler(request)

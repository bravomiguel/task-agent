from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Any, NotRequired, TYPE_CHECKING
import time
from langchain.agents.middleware import AgentMiddleware, AgentState
from typing import Callable, Awaitable
from langchain_core.messages import HumanMessage
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.runtime import Runtime
from deepagents_cli.agent_memory import AgentMemoryMiddleware as BaseAgentMemoryMiddleware


if TYPE_CHECKING:
    import modal


class ModalSandboxState(AgentState):
    """Extended state schema with Modal sandbox ID."""
    modal_sandbox_id: NotRequired[str]
    thread_id: NotRequired[str]


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
        volume_name: str = "agent-threads",
    ):
        super().__init__()
        self._workdir = workdir
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._max_timeout = max_timeout
        self._volume_name = volume_name

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

        # Get or create v2 volume for persistent thread storage
        volume = modal.Volume.from_name(
            self._volume_name,
            create_if_missing=True,
            version=2
        )

        # Create new sandbox with volume mounted and workdir set to thread folder
        app = modal.App.lookup("agent-sandbox", create_if_missing=True)
        sandbox = modal.Sandbox.create(
            app=app,
            workdir=f"/threads/{thread_id}",
            timeout=self._max_timeout,
            idle_timeout=self._idle_timeout,
            volumes={"/threads": volume},
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

        return {
            "modal_sandbox_id": sandbox.object_id,
            "thread_id": thread_id,
        }

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)


class ThreadContextMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that appends thread context to system prompt."""

    state_schema = ModalSandboxState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Append thread context to system prompt."""
        thread_id = request.state.get("thread_id")
        if thread_id and request.system_prompt:
            thread_context = (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/`."
            )
            request.system_prompt = request.system_prompt + thread_context
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Append thread context to system prompt."""
        thread_id = request.state.get("thread_id")
        if thread_id and request.system_prompt:
            thread_context = (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/`."
            )
            request.system_prompt = request.system_prompt + thread_context
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

        last_message = messages[-1]

        summary_prompt = f"""Based on this response, what does the user need to do next?
        Be concise and specific about what action or information is required.

        Response: {last_message.content}

        Provide a brief summary (1 sentence) of what the user needs to do next."""

        response = self.llm.invoke(
            [{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}

    async def aafter_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:
        """Async version: Generate review message after agent completes."""
        messages = state["messages"]

        if not messages:
            return {"review_message": "No action needed."}

        last_message = messages[-1]

        summary_prompt = f"""Based on this response, what does the user need to do next?
        Be concise and specific about what action or information is required.

        Response: {last_message.content}

        Provide a brief summary (1 sentence) of what the user needs to do next."""

        response = await self.llm.ainvoke([{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}


class AsyncAgentMemoryMiddleware(BaseAgentMemoryMiddleware):
    """Async-compatible version of AgentMemoryMiddleware."""

    async def abefore_agent(
        self,
        state,
        runtime,
    ):
        """(async) Load agent memory from file before agent execution."""
        if "agent_memory" not in state or state.get("agent_memory") is None:
            # Wrap blocking read in asyncio.to_thread
            file_data = await asyncio.to_thread(
                self.backend.read,
                "/agent.md"
            )
            return {"agent_memory": file_data}
        return None

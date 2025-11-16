from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from typing import Annotated, Any, NotRequired
from typing_extensions import TypedDict
import asyncio
from deepagents_cli.agent_memory import AgentMemoryMiddleware as BaseAgentMemoryMiddleware

import time
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from typing_extensions import NotRequired
from langgraph.channels.untracked_value import UntrackedValue

if TYPE_CHECKING:
    import modal
    from langgraph.runtime import Runtime


class ModalSandboxState(AgentState):
    """Extended state schema with Modal sandbox ID."""

    modal_sandbox_id: NotRequired[str]
    modal_app_name: NotRequired[str]


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
        app_name: str = "agent-sandbox",
    ):
        super().__init__()
        self._workdir = workdir
        self._startup_timeout = startup_timeout
        self._idle_timeout = idle_timeout
        self._max_timeout = max_timeout
        self._app_name = app_name

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

        # Create new sandbox
        app = modal.App.lookup(self._app_name, create_if_missing=True)
        sandbox = modal.Sandbox.create(
            app=app,
            workdir=self._workdir,
            timeout=self._max_timeout,
            idle_timeout=self._idle_timeout,
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

        return {
            "modal_sandbox_id": sandbox.object_id,
            "modal_app_name": self._app_name,
        }

    async def abefore_agent(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def get_sandbox(self, state: ModalSandboxState) -> modal.Sandbox:
        """Get the active sandbox for this thread by ID."""
        import modal

        sandbox_id = state.get("modal_sandbox_id")

        if not sandbox_id:
            raise RuntimeError("Modal sandbox not initialized")

        return modal.Sandbox.from_id(sandbox_id)


class BackendMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that provides CompositeBackend with Modal sandbox to tools.  

    This middleware reconstructs the ModalBackend from the sandbox ID  
    before each model call and wraps it in a CompositeBackend for routing.  
    """
    state_schema = ModalSandboxState

    def __init__(
        self,
        sandbox_middleware: ModalSandboxMiddleware,
        long_term_backend,  # FilesystemBackend for /memories/
    ):
        super().__init__()
        self._sandbox_middleware = sandbox_middleware
        self._long_term_backend = long_term_backend

    def before_model(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Inject CompositeBackend into state for tools to use (sync)."""
        # Import from your actual module paths
        from deepagents_cli.integrations.modal import ModalBackend
        from deepagents.backends import CompositeBackend    

        # Get sandbox for this thread
        sandbox = self._sandbox_middleware.get_sandbox(state)

        # Create ModalBackend
        modal_backend = ModalBackend(sandbox)

        # Create CompositeBackend with routing
        composite_backend = CompositeBackend(
            default=modal_backend,
            routes={"/memories/": self._long_term_backend},
        )

        # Store composite backend (wrapped in UntrackedValue)
        return {"modal_backend": UntrackedValue(composite_backend)}

    async def abefore_model(
        self, state: ModalSandboxState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Inject CompositeBackend into state for tools to use (async)."""
        # Delegate to sync version since get_sandbox and backend creation are sync
        return self.before_model(state, runtime)

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

        summary_prompt = f"""Review this conversation and summarize what input or request is needed from the human user.  
        Be concise and specific about what action or information is required.  
          
        Conversation:  
        {chr(10).join(f"{msg.type}: {msg.content}" for msg in messages[-5:])}  
          
        Provide a brief summary (1 sentence) of what the user needs to do next."""

        response = self.llm.invoke(
            [{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}

    async def aafter_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:
        """Async version: Generate review message after agent completes."""
        messages = state["messages"]

        if not messages:
            return {"review_message": "No action needed."}

        summary_prompt = f"""Review this conversation and summarize what input or request is needed from the human user.  
        Be concise and specific about what action or information is required.  
          
        Conversation:  
        {chr(10).join(f"{msg.type}: {msg.content}" for msg in messages[-5:])}  
          
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

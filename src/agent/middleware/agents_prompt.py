"""Middleware for loading AGENTS.md from volume and injecting into system prompt."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Awaitable, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime


# Local fallback path (repo version, used if volume file doesn't exist)
_LOCAL_AGENTS_MD = Path(__file__).parent.parent.parent.parent / "prompts" / "AGENTS.md"


class AgentsPromptState(AgentState):
    """Extended state with agents prompt content."""
    agents_prompt: NotRequired[str]


class AgentsPromptMiddleware(AgentMiddleware[AgentsPromptState, Any]):
    """Middleware that reads AGENTS.md from the volume and injects it into the system prompt.

    Reads /default-user/prompts/AGENTS.md from the sandbox once per agent run,
    caches in state, and injects into every model call. Falls back to the local
    repo version if the volume file doesn't exist.
    """

    state_schema = AgentsPromptState

    def before_agent(
        self, state: AgentsPromptState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Read AGENTS.md from sandbox, fall back to local file."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return self._load_local_fallback()

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            process = sandbox.exec(
                "cat", "/default-user/prompts/AGENTS.md", timeout=10
            )
            process.wait()

            if process.returncode == 0:
                content = process.stdout.read()
                if content.strip():
                    return {"agents_prompt": content}

            # File doesn't exist on volume, use local fallback
            return self._load_local_fallback()

        except Exception:
            return self._load_local_fallback()

    async def abefore_agent(
        self, state: AgentsPromptState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def _load_local_fallback(self) -> dict[str, Any] | None:
        """Load AGENTS.md from local repo as fallback."""
        try:
            content = _LOCAL_AGENTS_MD.read_text()
            return {"agents_prompt": content}
        except Exception:
            return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject AGENTS.md content into system prompt."""
        agents_prompt = request.state.get("agents_prompt")
        if agents_prompt and request.system_prompt:
            request.system_prompt = request.system_prompt + "\n\n" + agents_prompt

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject AGENTS.md content into system prompt."""
        agents_prompt = request.state.get("agents_prompt")
        if agents_prompt and request.system_prompt:
            request.system_prompt = request.system_prompt + "\n\n" + agents_prompt

        return await handler(request)

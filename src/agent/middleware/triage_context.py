"""Triage context middleware for injecting thread count into system prompt."""

from __future__ import annotations

from typing import Any, Callable, Awaitable, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse


class TriageContextState(AgentState):
    """State schema for triage context middleware."""
    active_thread_count: NotRequired[int]


class TriageContextMiddleware(AgentMiddleware[TriageContextState, Any]):
    """Middleware that injects active thread count into system prompt.

    Reads from state (set by TriageThreadsMiddleware) and prepends to system prompt.
    """

    state_schema = TriageContextState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject thread count into system prompt."""
        self._inject_context(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject thread count into system prompt."""
        self._inject_context(request)
        return await handler(request)

    def _inject_context(self, request: ModelRequest) -> None:
        """Prepend thread count to system prompt."""
        if not request.system_prompt:
            return

        active_thread_count = request.state.get("active_thread_count", 0)
        context = (
            f"## Active Threads\n\n"
            f"There are {active_thread_count} active threads in /workspace/threads/"
        )

        request.system_prompt = f"{context}\n\n---\n\n{request.system_prompt}"

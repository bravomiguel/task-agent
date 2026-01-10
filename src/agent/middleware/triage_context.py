"""Triage context middleware for injecting triage rules into system prompt."""

from __future__ import annotations

from typing import Any, Callable, Awaitable, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse


class TriageContextState(AgentState):
    """State schema for triage context middleware."""
    triage_rules: NotRequired[str]
    active_thread_count: NotRequired[int]


class TriageContextMiddleware(AgentMiddleware[TriageContextState, Any]):
    """Middleware that injects triage rules and active thread count into system prompt.

    Reads from state (set by TriageRulesMiddleware and TriageThreadsMiddleware)
    and prepends to system prompt for token caching benefits.
    """

    state_schema = TriageContextState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject triage context into system prompt."""
        self._inject_context(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject triage context into system prompt."""
        self._inject_context(request)
        return await handler(request)

    def _inject_context(self, request: ModelRequest) -> None:
        """Prepend triage rules and thread count to system prompt."""
        if not request.system_prompt:
            return

        context_parts = []

        # Add triage rules
        triage_rules = request.state.get("triage_rules")
        if triage_rules:
            context_parts.append(f"## Triage Rules\n\n{triage_rules}")

        # Add active thread count
        active_thread_count = request.state.get("active_thread_count", 0)
        context_parts.append(
            f"## Active Threads\n\n"
            f"There are {active_thread_count} active threads in /workspace/threads/"
        )

        if context_parts:
            context = "\n\n---\n\n".join(context_parts)
            request.system_prompt = f"{context}\n\n---\n\n{request.system_prompt}"

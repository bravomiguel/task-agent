"""Triage context middleware for injecting triage rules into system prompt."""

from __future__ import annotations

from typing import Any, Callable, Awaitable, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse


class TriageContextState(AgentState):
    """State schema for triage context middleware."""
    triage_rules: NotRequired[str]


class TriageContextMiddleware(AgentMiddleware[TriageContextState, Any]):
    """Middleware that injects triage rules into system prompt.

    Reads triage_rules from state (set by TriageRulesMiddleware) and
    prepends to system prompt for token caching benefits.
    """

    state_schema = TriageContextState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject triage rules into system prompt."""
        self._inject_rules(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject triage rules into system prompt."""
        self._inject_rules(request)
        return await handler(request)

    def _inject_rules(self, request: ModelRequest) -> None:
        """Prepend triage rules to system prompt if available."""
        if not request.system_prompt:
            return

        triage_rules = request.state.get("triage_rules")
        if triage_rules:
            request.system_prompt = (
                f"## Triage Rules\n\n{triage_rules}\n\n---\n\n{request.system_prompt}"
            )

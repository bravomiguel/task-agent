"""Triage filter middleware for early filtering before sandbox creation."""

from __future__ import annotations

from typing import Any, Literal, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langchain_openai import ChatOpenAI
from langgraph.runtime import Runtime
from pydantic import BaseModel, Field


class FilterDecision(BaseModel):
    """Structured output for filter decision."""

    decision: Literal["filter_out", "process"] = Field(
        description="'filter_out' to discard the event, 'process' to continue to agent"
    )


FILTER_SYSTEM_PROMPT = """You are a triage filter that decides whether incoming events should be processed or filtered out.

## Triage Rules

{triage_rules}

## Your Task

Based on the rules above, decide if this event should be:
- **filter_out**: Discard the event (matches filter criteria)
- **process**: Continue processing (requires action)

Respond with your decision."""


class TriageFilterState(AgentState):
    """State schema for triage filter middleware."""

    triage_rules: NotRequired[str]


class TriageFilterMiddleware(AgentMiddleware[TriageFilterState, Any]):
    """Middleware that filters events before sandbox creation.

    Reads triage rules via Modal function (no sandbox needed),
    makes LLM call to decide filter_out vs process, and terminates
    the run early if filtering out to save costs.

    Must run BEFORE ModalSandboxMiddleware.
    """

    state_schema = TriageFilterState

    def __init__(
        self,
        model: str = "gpt-4.1-mini",
    ):
        super().__init__()
        self._llm = ChatOpenAI(model=model).with_structured_output(FilterDecision)

    def _read_triage_rules(self) -> str | None:
        """Read triage rules via Modal function."""
        try:
            fn = modal.Function.from_name("file-service", "read_triage_rules")
            return fn.remote()
        except Exception as e:
            print(f"Warning: Could not read triage rules: {e}")
            return None

    def _extract_event_content(self, messages: list) -> str | None:
        """Extract the event content from the first user message."""
        for msg in messages:
            msg_type = getattr(msg, "type", None) or (
                msg.get("type") if isinstance(msg, dict) else None
            )
            msg_role = getattr(msg, "role", None) or (
                msg.get("role") if isinstance(msg, dict) else None
            )

            if msg_type == "human" or msg_role == "user":
                content = getattr(msg, "content", None) or (
                    msg.get("content", "") if isinstance(msg, dict) else ""
                )
                return content
        return None

    def _get_filter_decision(self, event_content: str, triage_rules: str) -> str:
        """Make LLM call to get filter decision."""
        system_prompt = FILTER_SYSTEM_PROMPT.format(triage_rules=triage_rules)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": event_content},
        ]

        response: FilterDecision = self._llm.invoke(messages)
        return response.decision

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self, state: TriageFilterState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Filter events before sandbox creation."""
        # Read triage rules from volume
        triage_rules = self._read_triage_rules()
        if not triage_rules:
            # If we can't read rules, continue to agent
            print("Warning: No triage rules found, continuing to agent")
            return None

        # Get event content from messages
        messages = state.get("messages", [])
        event_content = self._extract_event_content(messages)
        if not event_content:
            print("Warning: No event content found, continuing to agent")
            return {"triage_rules": triage_rules}

        # Get filter decision from LLM
        try:
            decision = self._get_filter_decision(event_content, triage_rules)
        except Exception as e:
            print(f"Warning: Filter LLM call failed: {e}, continuing to agent")
            return {"triage_rules": triage_rules}

        if decision == "filter_out":
            print("Triage: Event filtered out by TriageFilterMiddleware")
            return {"jump_to": "end"}

        # Continue to agent, pass triage_rules for context middleware
        print("Triage: Event passed filter, continuing to agent")
        return {"triage_rules": triage_rules}

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: TriageFilterState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version - delegates to sync for now."""
        return self.before_agent(state, runtime)

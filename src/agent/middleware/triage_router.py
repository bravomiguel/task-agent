"""Triage router middleware for executing routing decisions."""

from __future__ import annotations

import os
import re
from typing import Any, NotRequired

import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime

from agent.triage_schema import TriageDecision


# LangGraph API URL - use internal URL since middleware runs on same server
LANGGRAPH_API_URL = os.getenv("LANGGRAPH_INTERNAL_URL", "http://localhost:2024")


class TriageRouterState(AgentState):
    """State schema for triage router middleware."""

    structured_response: NotRequired[TriageDecision]


class TriageRouterMiddleware(AgentMiddleware[TriageRouterState, Any]):
    """Middleware that executes routing decisions after agent completes.

    Reads the structured response from state (TriageDecision):
    - action="filter_out" - do nothing
    - action="route", thread_id="new" - create new thread, kick off run
    - action="route", thread_id="<uuid>" - kick off run on existing thread

    Must run as after_agent middleware.
    """

    state_schema = TriageRouterState

    def __init__(self, api_url: str | None = None):
        super().__init__()
        self._api_url = api_url or LANGGRAPH_API_URL

    def after_agent(
        self, state: TriageRouterState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Execute routing based on structured response."""
        # Get structured decision from state
        decision = state.get("structured_response")
        if decision is None:
            print("Warning: No structured_response in state, cannot route")
            return None

        # Handle filter_out action
        if decision.action == "filter_out":
            print("Triage: Event filtered out")
            return None

        # Handle route action
        if decision.action != "route":
            print(f"Warning: Unknown action '{decision.action}'")
            return None

        if not decision.thread_id:
            print("Warning: No thread_id in routing decision")
            return None

        # Get the original event from first user message
        messages = state.get("messages", [])
        event_content = self._extract_event_content(messages)
        if not event_content:
            print("Warning: Could not extract event content")
            return None

        # Format message content for the task agent
        message_content = self._format_message_for_task_agent(event_content)

        # Execute routing
        try:
            thread_id = decision.thread_id

            if thread_id == "new":
                # Create new thread then kick off run
                thread_id = self._create_thread()
                print(f"Triage: Created new thread {thread_id}")

            # Kick off run on thread
            self._create_run(thread_id, message_content)
            print(f"Triage: Kicked off run on thread {thread_id}")
        except Exception as e:
            print(f"Error executing routing: {e}")

        return None

    def _extract_event_content(self, messages: list) -> str | None:
        """Extract the original event from the first user message."""
        for msg in messages:
            # Handle both dict-style and LangChain message objects
            msg_type = getattr(msg, "type", None) or (msg.get("type") if isinstance(msg, dict) else None)
            msg_role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)

            if msg_type == "human" or msg_role == "user":
                content = getattr(msg, "content", None) or (msg.get("content", "") if isinstance(msg, dict) else "")
                return content
        return None

    def _format_message_for_task_agent(self, event_content: str) -> str:
        """Format the event content for the task agent."""
        # Parse XML-style event to extract fields
        from_match = re.search(r"<from>(.*?)</from>", event_content, re.DOTALL)
        subject_match = re.search(r"<subject>(.*?)</subject>", event_content, re.DOTALL)
        body_match = re.search(r"<body>(.*?)</body>", event_content, re.DOTALL)

        from_addr = from_match.group(1).strip() if from_match else "Unknown"
        subject = subject_match.group(1).strip() if subject_match else "No subject"
        body = body_match.group(1).strip() if body_match else event_content

        return f"New task from email:\n\nFrom: {from_addr}\nSubject: {subject}\n\n{body}"

    def _create_thread(self) -> str:
        """Create a new thread via LangGraph API."""
        response = httpx.post(
            f"{self._api_url}/threads",
            headers={
                "Authorization": "Bearer 123",
                "Content-Type": "application/json",
            },
            json={},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["thread_id"]

    def _create_run(self, thread_id: str, message_content: str) -> None:
        """Create a run on a thread via LangGraph API."""
        response = httpx.post(
            f"{self._api_url}/threads/{thread_id}/runs",
            headers={
                "Authorization": "Bearer 123",
                "Content-Type": "application/json",
            },
            json={
                "assistant_id": "task_agent",
                "input": {
                    "messages": [
                        {
                            "role": "user",
                            "content": message_content,
                        }
                    ]
                },
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()

    async def aafter_agent(
        self, state: TriageRouterState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.after_agent(state, runtime)

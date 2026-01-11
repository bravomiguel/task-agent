"""Agent tools."""

from __future__ import annotations

import os
import re
from typing import Annotated

import httpx
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState


LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://localhost:2024")


def _extract_event_content(messages: list) -> str | None:
    """Extract the original event from the first user message."""
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


def _format_message_for_task_agent(event_content: str) -> str:
    """Format the event content for the task agent."""
    from_match = re.search(r"<from>(.*?)</from>", event_content, re.DOTALL)
    subject_match = re.search(r"<subject>(.*?)</subject>", event_content, re.DOTALL)
    body_match = re.search(r"<body>(.*?)</body>", event_content, re.DOTALL)

    from_addr = from_match.group(1).strip() if from_match else "Unknown"
    subject = subject_match.group(1).strip() if subject_match else "No subject"
    body = body_match.group(1).strip() if body_match else event_content

    return f"New task from email:\n\nFrom: {from_addr}\nSubject: {subject}\n\n{body}"


@tool
def route_event(
    thread_id: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Route the incoming event to a thread and start the task agent.

    Call this tool when you have decided which thread to route the event to.
    The tool will execute the routing and return success or an error message.
    If you get an error, you may retry up to 2 more times.

    Args:
        thread_id: Use 'new' for a new thread, or provide an existing thread UUID.

    Returns:
        Success message with details, or error message if something went wrong.
    """
    api_url = LANGGRAPH_API_URL

    if not thread_id:
        return "Error: thread_id is required. Use 'new' or an existing thread UUID."

    # Extract event content from messages in state
    messages = state.get("messages", []) if state else []
    event_content = _extract_event_content(messages)
    if not event_content:
        return "Error: Could not extract event content from messages."

    # Format message for task agent
    message_content = _format_message_for_task_agent(event_content)

    # Execute routing
    try:
        target_thread_id = thread_id

        if thread_id == "new":
            # Create new thread
            response = httpx.post(
                f"{api_url}/threads",
                headers={"Content-Type": "application/json"},
                json={},
                timeout=30,
            )
            response.raise_for_status()
            target_thread_id = response.json()["thread_id"]

        # Create run on thread
        response = httpx.post(
            f"{api_url}/threads/{target_thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "task_agent",
                "input": {"messages": [{"role": "user", "content": message_content}]},
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()

        if thread_id == "new":
            return f"Success: Created new thread {target_thread_id} and started task agent."
        else:
            return f"Success: Routed to existing thread {target_thread_id} and started task agent."

    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out. The API at {api_url} may be slow or unavailable."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: Unexpected error during routing: {e}"

"""Middleware for detecting events in user messages and tracking them in state."""

from __future__ import annotations

import re
import html
from typing import Any, NotRequired
from typing_extensions import TypedDict, Annotated

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import BaseMessage
from langgraph.runtime import Runtime

from agent.middleware.modal_sandbox import ModalSandboxState


class EventInfo(TypedDict):
    """Event information for display in frontend."""

    source: str  # gmail, outlook, slack, teams, google-meet, zoom, recorder, calendar
    url: str  # Deep link to open event


def events_reducer(
    left: list[EventInfo] | None, right: list[EventInfo] | None
) -> list[EventInfo]:
    """Reducer that appends new events to existing list."""
    left_list = left or []
    right_list = right or []
    # Append new events, avoiding duplicates by URL
    existing_urls = {e["url"] for e in left_list}
    new_events = [e for e in right_list if e["url"] not in existing_urls]
    return left_list + new_events


class EventsState(ModalSandboxState):
    """Extended state schema with events list."""

    events: Annotated[NotRequired[list[EventInfo]], events_reducer]


def _unescape_xml(text: str) -> str:
    """Unescape XML entities."""
    return html.unescape(text)


def _extract_xml_content(xml: str, tag: str) -> str:
    """Extract text content from an XML element."""
    match = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", xml)
    return _unescape_xml(match.group(1).strip()) if match else ""


def _build_email_url(source: str, message_id: str) -> str | None:
    """Build URL for email events."""
    if not message_id:
        return None

    if source == "gmail":
        return f"https://mail.google.com/mail/u/0/#all/{message_id}"
    elif source == "outlook":
        from urllib.parse import quote
        return f"https://outlook.office.com/mail/deeplink/read/{quote(message_id)}"

    return None


def _build_chat_url(source: str, channel_id: str, timestamp: str, chat_id: str, message_id: str) -> str | None:
    """Build URL for chat events."""
    if source == "slack":
        if channel_id and timestamp:
            return f"https://slack.com/app_redirect?channel={channel_id}&message_ts={timestamp}"
    elif source == "teams":
        if chat_id and message_id:
            return f"https://teams.microsoft.com/l/message/{chat_id}/{message_id}"

    return None


def _build_meeting_url(meeting_id: str) -> str | None:
    """Build URL for meeting events (internal route)."""
    if meeting_id:
        return f"/meetings?id={meeting_id}"
    return None


def _extract_events_from_content(content: str) -> list[EventInfo]:
    """Parse XML events from message content and build EventInfo list."""
    events: list[EventInfo] = []

    # Parse email events
    email_regex = r'<email\s+source="([^"]*)">([\s\S]*?)</email>'
    for match in re.finditer(email_regex, content):
        source = match.group(1)
        inner = match.group(2)
        message_id = _extract_xml_content(inner, "message_id")

        url = _build_email_url(source, message_id)
        if url:
            events.append({"source": source, "url": url})

    # Parse chat events
    chat_regex = r'<chat\s+source="([^"]*)">([\s\S]*?)</chat>'
    for match in re.finditer(chat_regex, content):
        source = match.group(1)
        inner = match.group(2)

        # Extract IDs needed for URL construction
        channel_id = _extract_xml_content(inner, "channel")  # Slack channel
        timestamp = _extract_xml_content(inner, "ts")
        chat_id = _extract_xml_content(inner, "chat_id")  # Teams chat
        message_id = _extract_xml_content(inner, "message_id")  # Teams message

        url = _build_chat_url(source, channel_id, timestamp, chat_id, message_id)
        if url:
            events.append({"source": source, "url": url})

    # Parse meeting events
    meeting_regex = r'<meeting\s+source="([^"]*)">([\s\S]*?)</meeting>'
    for match in re.finditer(meeting_regex, content):
        source_attr = match.group(1)
        inner = match.group(2)

        # Determine source from platform or source attribute
        platform = _extract_xml_content(inner, "platform")
        if platform in ("google-meet", "zoom", "teams"):
            source = platform
        elif source_attr in ("recorder", "manual"):
            source = "recorder"
        elif source_attr in ("google-calendar", "calendar"):
            source = "calendar"
        else:
            source = "unknown"

        meeting_id = _extract_xml_content(inner, "id")
        url = _build_meeting_url(meeting_id)
        if url:
            events.append({"source": source, "url": url})

    return events


def _get_latest_user_message_content(state: dict) -> str | None:
    """Extract content from the latest user message in state."""
    messages = state.get("messages", [])
    # Iterate in reverse to find the last user message
    for msg in reversed(messages):
        # Handle both LangChain message objects and dicts
        if isinstance(msg, BaseMessage):
            if msg.type == "human":
                return msg.content if isinstance(msg.content, str) else str(msg.content)
        elif isinstance(msg, dict):
            msg_type = msg.get("type")
            msg_role = msg.get("role")
            if msg_type == "human" or msg_role == "user":
                content = msg.get("content", "")
                return content if isinstance(content, str) else str(content)
    return None


class EventDetectionMiddleware(AgentMiddleware[EventsState, Any]):
    """Middleware that detects events in user messages and adds them to state.

    Parses XML event tags (email, chat, meeting) from the first user message
    and extracts source and URL for each event. Events are appended to the
    state's events list for display in the frontend.
    """

    state_schema = EventsState

    def before_agent(
        self, state: EventsState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Detect events in latest user message and add to state."""
        content = _get_latest_user_message_content(state)
        if not content:
            return None

        events = _extract_events_from_content(content)
        if not events:
            return None

        print(f"[EventDetectionMiddleware] Detected {len(events)} events: {events}")
        return {"events": events}

    async def abefore_agent(
        self, state: EventsState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

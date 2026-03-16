"""Meeting ingest middleware — truncate inline transcript in agent messages.

When the agent receives a meeting-transcript inbound event, this middleware
truncates the inline transcript to keep the context window manageable.
The full transcript is saved to /mnt/meeting-transcripts/ by the indexer
(synced from Supabase Storage where the Electron app uploaded it).

The transcript_path attribute is already set by the queue-dispatcher.

Must run AFTER ModalSandboxMiddleware (needs sandbox).
"""

from __future__ import annotations

import logging
import re
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

# Truncate inline transcript to ~8K chars (keeps context window manageable;
# full transcript is persisted on volume and searchable via memory_search)
INLINE_TRANSCRIPT_MAX_CHARS = 8_000


class MeetingIngestState(AgentState):
    """State fields for meeting ingest."""

    channel_platform: NotRequired[str]
    channel_metadata: NotRequired[dict]


def _truncate_meeting_transcript(messages: list) -> None:
    """Find the meeting-transcript system message and truncate the inline transcript."""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content", "")
        if not isinstance(content, str) or 'type="meeting-transcript"' not in content:
            continue

        # Truncate the transcript body inside the tag
        def _truncate_body(m: re.Match) -> str:
            tag_open = m.group(1)
            body = m.group(2)
            tag_close = m.group(3)

            if len(body) <= INLINE_TRANSCRIPT_MAX_CHARS:
                return m.group(0)

            truncated = body[:INLINE_TRANSCRIPT_MAX_CHARS]
            # Cut at last newline to avoid mid-line truncation
            last_nl = truncated.rfind("\n")
            if last_nl > INLINE_TRANSCRIPT_MAX_CHARS // 2:
                truncated = truncated[:last_nl]

            remaining_chars = len(body) - len(truncated)
            note = f"\n\n[truncated — {remaining_chars} characters remaining]"
            return tag_open + truncated + note + "\n" + tag_close

        updated = re.sub(
            r'(<system-message[^>]*>)(.*?)(</system-message>)',
            _truncate_body,
            content,
            flags=re.DOTALL,
        )

        # Write back
        if hasattr(msg, "content"):
            msg.content = updated
        elif isinstance(msg, dict):
            msg["content"] = updated
        return


class MeetingIngestMiddleware(AgentMiddleware[MeetingIngestState, Any]):
    """Truncate inline meeting transcripts in agent messages."""

    state_schema = MeetingIngestState

    def before_agent(
        self, state: MeetingIngestState, runtime: Runtime
    ) -> dict[str, Any] | None:
        if state.get("channel_platform") != "meeting":
            return None

        messages = state.get("messages", [])
        _truncate_meeting_transcript(messages)
        return None

    async def abefore_agent(
        self, state: MeetingIngestState, runtime: Runtime
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

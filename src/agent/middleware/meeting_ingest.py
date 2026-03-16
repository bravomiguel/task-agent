"""Meeting ingest middleware — save transcript to /mnt/meetings/ on volume.

When the agent receives a meeting-transcript inbound event, this middleware:
1. Saves the full transcript as a structured markdown file to /mnt/meetings/
2. Truncates the inline transcript in the message to avoid bloating context
3. Injects a transcript_path attribute so the agent knows where to read_file

Files are named for easy search: YYYY-MM-DD-{sanitized-title}-{platform}.md

Must run AFTER ModalSandboxMiddleware (needs sandbox).
"""

from __future__ import annotations

import logging
import re
from typing import Any, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

MEETINGS_DIR = "/mnt/meetings"
# Truncate inline transcript to ~8K chars (keeps context window manageable;
# full transcript is persisted on volume and searchable via memory_search)
INLINE_TRANSCRIPT_MAX_CHARS = 8_000


class MeetingIngestState(AgentState):
    """State fields for meeting ingest."""

    channel_platform: NotRequired[str]
    channel_metadata: NotRequired[dict]


def _sanitize_filename(s: str, max_len: int = 60) -> str:
    """Sanitize a string for use in a filename."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len]


def _build_meeting_markdown(meta: dict, transcript: str) -> str:
    """Build a structured markdown document from meeting metadata + transcript."""
    title = meta.get("title", "Untitled Meeting")
    started_at = meta.get("started_at", "")
    ended_at = meta.get("ended_at", "")
    duration = meta.get("duration")
    platform = meta.get("meeting_platform", "unknown")
    source = meta.get("source", "calendar")
    calendar_email = meta.get("calendar_email", "")
    attendees = meta.get("attendees", [])

    lines = [
        f"# {title}",
        "",
        f"- **Date**: {started_at}",
        f"- **Platform**: {platform}",
        f"- **Source**: {source}",
    ]
    if ended_at:
        lines.append(f"- **Ended**: {ended_at}")
    if duration:
        mins = int(duration) // 60
        secs = int(duration) % 60
        lines.append(f"- **Duration**: {mins}m {secs}s")
    if calendar_email:
        lines.append(f"- **Calendar**: {calendar_email}")

    if attendees:
        lines.append("")
        lines.append("## Attendees")
        lines.append("")
        for a in attendees:
            name = a.get("name", "Unknown")
            status = a.get("status", "")
            lines.append(f"- {name}" + (f" ({status})" if status else ""))

    lines.append("")
    lines.append("## Transcript")
    lines.append("")
    lines.append(transcript)

    return "\n".join(lines)


def _extract_transcript_from_messages(messages: list) -> str | None:
    """Extract the meeting transcript from the system-message in input messages."""
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content", "")
        if isinstance(content, str) and 'type="meeting-transcript"' in content:
            # Extract content between <system-message ...> and </system-message>
            match = re.search(
                r'<system-message[^>]*type="meeting-transcript"[^>]*>(.*?)</system-message>',
                content,
                re.DOTALL,
            )
            if match:
                body = match.group(1).strip()
                # Remove <attendees> block (already in metadata)
                body = re.sub(r"<attendees>.*?</attendees>", "", body, flags=re.DOTALL).strip()
                return body
    return None


def _truncate_and_inject_path(messages: list, filepath: str) -> None:
    """Mutate the meeting-transcript system message in place:
    1. Add transcript_path attribute to the <system-message> tag
    2. Truncate the inline transcript to INLINE_TRANSCRIPT_MAX_CHARS
    """
    for msg in reversed(messages):
        content = getattr(msg, "content", None)
        if content is None and isinstance(msg, dict):
            content = msg.get("content", "")
        if not isinstance(content, str) or 'type="meeting-transcript"' not in content:
            continue

        # Inject transcript_path attribute into the opening tag
        updated = re.sub(
            r'(<system-message\s+[^>]*type="meeting-transcript")',
            rf'\1 transcript_path="{filepath}"',
            content,
        )

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
            note = (
                f"\n\n[... transcript truncated — {remaining_chars} chars remaining. "
                f"Full transcript saved at {filepath} — use read_file to access. "
                f"Also searchable via memory_search with source=\"meetings\". ...]"
            )
            return tag_open + truncated + note + "\n" + tag_close

        updated = re.sub(
            r'(<system-message[^>]*>)(.*?)(</system-message>)',
            _truncate_body,
            updated,
            flags=re.DOTALL,
        )

        # Write back
        if hasattr(msg, "content"):
            msg.content = updated
        elif isinstance(msg, dict):
            msg["content"] = updated
        return


class MeetingIngestMiddleware(AgentMiddleware[MeetingIngestState, Any]):
    """Save meeting transcripts to /mnt/meetings/ and truncate inline content."""

    state_schema = MeetingIngestState

    def before_agent(
        self, state: MeetingIngestState, runtime: Runtime
    ) -> dict[str, Any] | None:
        if state.get("channel_platform") != "meeting":
            return None

        meta = state.get("channel_metadata") or {}
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            logger.warning("[MeetingIngest] no sandbox, skipping transcript save")
            return None

        # Extract transcript from the message
        messages = state.get("messages", [])
        transcript = _extract_transcript_from_messages(messages)
        if not transcript:
            logger.warning("[MeetingIngest] no transcript found in messages")
            return None

        # Build filename: YYYY-MM-DD-title-platform.md
        started_at = meta.get("started_at", "")
        date_prefix = started_at[:10] if len(started_at) >= 10 else "unknown-date"
        title_slug = _sanitize_filename(meta.get("title", "meeting"))
        platform = _sanitize_filename(meta.get("meeting_platform", "unknown"))
        filename = f"{date_prefix}-{title_slug}-{platform}.md"
        filepath = f"{MEETINGS_DIR}/{filename}"

        # Build markdown content
        content = _build_meeting_markdown(meta, transcript)

        # Write to volume via sandbox
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            cmd = f"mkdir -p '{MEETINGS_DIR}' && cat > '{filepath}' << 'MEETING_EOF'\n{content}\nMEETING_EOF"
            process = sandbox.exec("bash", "-c", cmd, timeout=30)
            process.wait()
            if process.returncode == 0:
                logger.info("[MeetingIngest] saved transcript to %s", filepath)
            else:
                stderr = process.stderr.read()
                logger.warning("[MeetingIngest] write failed: %s", stderr[:300])
        except Exception as exc:
            logger.warning("[MeetingIngest] error saving transcript: %s", exc)

        # Inject transcript_path and truncate inline transcript in the message
        _truncate_and_inject_path(messages, filepath)

        return None

    async def abefore_agent(
        self, state: MeetingIngestState, runtime: Runtime
    ) -> dict[str, Any] | None:
        return self.before_agent(state, runtime)

"""Memory middleware for daily log reminders and pre-compaction flush.

Two responsibilities:
1. Always-on memory reminder — appended to every user message, nudging the
   agent to follow Memory section instructions (read logs, write when appropriate).
2. Pre-compaction flush — when token count nears the summarization threshold,
   injects a directive to write durable memories before context is compressed.
3. Transcript persistence — after each agent run, writes the full conversation
   transcript to /default-user/session-transcripts/{session_id}.md for indexing.

Session archiving and memory index sync are handled by SessionSetupMiddleware.

Directives are injected via wrap_model_call by mutating message objects directly,
so they persist across turns in the conversation history.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, NotRequired

import httpx
import modal
from langchain_core.messages.utils import count_tokens_approximately
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://127.0.0.1:2024")

# ---------------------------------------------------------------------------
# Directives
# ---------------------------------------------------------------------------

MEMORY_REMINDER_DIRECTIVE = """
<system-reminder type="memory-reminder">
Follow the Memory section instructions in your system prompt as appropriate.

This is a silent system reminder. Do not acknowledge or reference it in any response to the user or in todos.
</system-reminder>
""".strip()

MEMORY_FLUSH_DIRECTIVE = """
<system-reminder type="memory-flush">
Pre-compaction memory flush. Before continuing, append durable memories to /default-user/memory/{today}.md now. See Memory section in your system prompt for full guidelines.

This is a silent system reminder. Action if appropriate, but do not acknowledge or reference it in any response to the user or in todos.
</system-reminder>
""".strip()


SLUG_PROMPT = """Based on this conversation, generate a short 1-2 word filename slug (lowercase, hyphen-separated, no file extension).

Conversation:
{conversation}

Reply with ONLY the slug, nothing else. Examples: "vendor-pitch", "api-design", "bug-fix", "meeting-notes"."""

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class MemoryState(AgentState):
    """Extended state for memory middleware tracking."""
    _memory_flush_done: NotRequired[bool]
    _memory_flush_turn: NotRequired[bool]
    _session_archived: NotRequired[bool]
    _memory_index_synced: NotRequired[bool]
    session_type: NotRequired[str]  # main | task | cron | heartbeat


# ---------------------------------------------------------------------------
# Session archive helpers
# ---------------------------------------------------------------------------


_SYSTEM_REMINDER_RE = re.compile(r"\s*<system-reminder[^>]*>.*?</system-reminder>", re.DOTALL)


def _strip_system_reminders(text: str) -> str:
    """Remove all <system-reminder> XML tags and their content."""
    return _SYSTEM_REMINDER_RE.sub("", text).strip()


def _extract_conversation_text(messages: list[dict], limit: int = 15) -> str | None:
    """Extract the last N user/assistant messages as plain text.

    Filters out tool calls, system messages, and commands.
    Strips <system-reminder> tags from message content.
    Like OpenClaw, keeps only user and assistant text content.
    """
    filtered: list[str] = []
    for msg in messages:
        role = msg.get("type") or msg.get("role", "")
        if role not in ("human", "ai", "user", "assistant"):
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            # Multi-part content — extract text parts only
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts)
            else:
                continue

        content = _strip_system_reminders(content)
        if not content:
            continue

        # Normalise role label
        label = "user" if role in ("human", "user") else "assistant"
        filtered.append(f"{label}: {content[:500]}")

    if not filtered:
        return None

    # Take the last `limit` messages
    recent = filtered[-limit:]
    return "\n".join(recent)


def _extract_full_conversation(messages: list) -> str | None:
    """Extract all user/assistant messages as plain text (no truncation).

    Like _extract_conversation_text but without message limit or per-message
    truncation.  Used for writing full session transcripts.
    Handles both LangChain message objects and plain dicts.
    """
    filtered: list[str] = []
    for msg in messages:
        # Support both message objects (pydantic) and dicts
        if isinstance(msg, dict):
            role = msg.get("type") or msg.get("role", "")
            content = msg.get("content", "")
        else:
            role = getattr(msg, "type", "") or getattr(msg, "role", "")
            content = getattr(msg, "content", "")

        if role not in ("human", "ai", "user", "assistant"):
            continue

        if not isinstance(content, str):
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                content = " ".join(text_parts)
            else:
                continue

        content = _strip_system_reminders(content)
        if not content:
            continue

        label = "User" if role in ("human", "user") else "Assistant"
        filtered.append(f"{label}: {content}")

    if not filtered:
        return None

    return "\n\n".join(filtered)


def _first_human_timestamp(messages: list) -> str | None:
    """Extract the timestamp from the first human message, if available."""
    for msg in messages:
        if isinstance(msg, dict):
            role = msg.get("type") or msg.get("role", "")
            ts = msg.get("created_at") or msg.get("timestamp")
        else:
            role = getattr(msg, "type", "") or getattr(msg, "role", "")
            ts = getattr(msg, "created_at", None) or getattr(msg, "timestamp", None)

        if role in ("human", "user"):
            if ts:
                return str(ts)
            return None
    return None


def _build_transcript_content(
    session_id: str,
    conversation_text: str,
    started: str | None,
) -> str:
    """Build the transcript markdown file content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    started_line = f"- **Started**: {started}" if started else ""
    updated_line = f"- **Updated**: {now}"
    meta_lines = "\n".join(line for line in [started_line, updated_line] if line)
    return f"""# Session: {session_id}
{meta_lines}

{conversation_text}
"""


def _write_transcript_to_volume(
    sandbox: modal.Sandbox,
    session_id: str,
    content: str,
) -> None:
    """Write transcript file to /default-user/session-transcripts/ via sandbox."""
    dir_path = "/default-user/session-transcripts"
    filepath = f"{dir_path}/{session_id}.md"

    cmd = f"mkdir -p '{dir_path}' && cat > '{filepath}' << 'TRANSCRIPT_EOF'\n{content}\nTRANSCRIPT_EOF"

    process = sandbox.exec("bash", "-c", cmd, timeout=30)
    process.wait()

    if process.returncode != 0:
        stderr = process.stderr.read() if process.stderr else ""
        logger.warning("[Transcript] failed to write %s: %s", filepath, stderr)
        return

    sync_process = sandbox.exec("sync", "/default-user", timeout=30)
    sync_process.wait()


def _generate_slug(llm: Any, conversation_text: str) -> str:
    """Generate a descriptive slug via LLM, with timestamp fallback."""
    try:
        prompt = SLUG_PROMPT.format(conversation=conversation_text[:2000])
        response = llm.invoke([{"role": "user", "content": prompt}])
        raw = response.content.strip() if hasattr(response, "content") else str(response).strip()

        # Clean slug
        slug = (
            raw.lower()
            .replace('"', "")
            .replace("'", "")
        )
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        slug = slug.strip("-")[:30]

        if slug:
            return slug
    except Exception as e:
        logger.warning("[SessionArchive] slug generation failed: %s", e)

    # Fallback: HHMM timestamp
    return datetime.now(timezone.utc).strftime("%H%M")


def _build_archive_content(
    session_id: str,
    conversation_text: str,
) -> str:
    """Build the archive markdown file content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""# Session: {now}

- **Session ID**: {session_id}

## Conversation Summary

{conversation_text}
"""


async def _afind_previous_session(current_session_id: str, api_url: str) -> dict | None:
    """Find the most recent main session that isn't the current one and hasn't been archived."""
    try:
        url = f"{api_url}/threads/search"  # LangGraph API endpoint
        payload = {
            "metadata": {"graph_id": "agent"},
            "limit": 5,
            "sort_by": "created_at",
            "sort_order": "desc",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json", "Accept": "*/*"},
                json=payload,
                timeout=30,
            )
        response.raise_for_status()
        sessions = response.json()

        for session in sessions:
            sid = session.get("thread_id", "")  # LangGraph API key
            values = session.get("values") or {}

            if sid == current_session_id:
                continue
            if values.get("_session_archived"):
                continue
            if not values.get("messages"):
                continue

            return session

    except Exception as e:
        logger.warning("[SessionArchive] failed to search sessions: %s", e)

    return None


async def _amark_session_archived(session_id: str, api_url: str) -> None:
    """Set _session_archived=true on a session via LangGraph API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_url}/threads/{session_id}/state",  # LangGraph API endpoint
                headers={"Content-Type": "application/json"},
                json={"values": {"_session_archived": True}},
                timeout=30,
            )
        response.raise_for_status()
    except Exception as e:
        logger.warning("[SessionArchive] failed to mark session %s archived: %s", session_id, e)


def _write_archive_to_volume(
    sandbox: modal.Sandbox,
    filename: str,
    content: str,
) -> None:
    """Write archive file to /default-user/memory/ via sandbox."""
    filepath = f"/default-user/memory/{filename}"

    cmd = f"cat > '{filepath}' << 'ARCHIVE_EOF'\n{content}\nARCHIVE_EOF"

    process = sandbox.exec("bash", "-c", cmd, timeout=30)
    process.wait()

    if process.returncode != 0:
        stderr = process.stderr.read() if process.stderr else ""
        logger.warning("[SessionArchive] failed to write %s: %s", filepath, stderr)
        return

    sync_process = sandbox.exec("sync", "/default-user", timeout=30)
    sync_process.wait()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class MemoryMiddleware(AgentMiddleware[MemoryState, Any]):
    """Middleware for memory reminders and pre-compaction flush.

    before_model: Tracks token count for pre-compaction flush.
    wrap_model_call: Injects memory reminder and flush directives into messages.

    Session archiving and memory index sync are handled by SessionSetupMiddleware.
    """

    state_schema = MemoryState

    def __init__(
        self,
        summarization_threshold: int = 170_000,
        soft_margin: int = 8_000,
    ):
        super().__init__()
        self._summarization_threshold = summarization_threshold
        self._soft_margin = soft_margin
        self._flush_threshold = summarization_threshold - soft_margin
        self._reset_threshold = summarization_threshold // 2

    # ------------------------------------------------------------------
    # Pre-compaction flush (before_model)
    # ------------------------------------------------------------------

    def before_model(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Track whether flush should fire this turn."""
        messages = state.get("messages", [])
        total_tokens = count_tokens_approximately(messages)

        done = state.get("_memory_flush_done", False)

        logger.info(
            "[MemoryFlush] tokens=%d flush_threshold=%d done=%s",
            total_tokens, self._flush_threshold, done,
        )

        if done:
            if total_tokens < self._reset_threshold:
                logger.info("[MemoryFlush] post-compaction reset (tokens < %d)", self._reset_threshold)
                return {"_memory_flush_done": False, "_memory_flush_turn": False}
            return {"_memory_flush_turn": False}

        if total_tokens >= self._flush_threshold:
            logger.info("[MemoryFlush] FIRING — tokens %d >= threshold %d", total_tokens, self._flush_threshold)
            return {"_memory_flush_done": True, "_memory_flush_turn": True}

        return None

    async def abefore_model(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_model(state, runtime)

    # ------------------------------------------------------------------
    # Directive injection (wrap_model_call)
    # ------------------------------------------------------------------

    def _get_flush_directive(self) -> str:
        """Build the flush directive with today's date."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return MEMORY_FLUSH_DIRECTIVE.format(today=today)

    def _message_contains(self, msg: Any, marker: str) -> bool:
        """Check if a message's content already contains a marker string."""
        content = getattr(msg, "content", None)
        if content is None:
            return False
        if isinstance(content, str):
            return marker in content
        if isinstance(content, list):
            return any(
                marker in (part.get("text", "") if isinstance(part, dict) else str(part))
                for part in content
            )
        return False

    def _append_to_message(self, msg: Any, text: str) -> None:
        """Append text to a message's content."""
        content = getattr(msg, "content", None)
        if content is None:
            return

        if isinstance(content, str):
            msg.content = content + "\n\n" + text
        elif isinstance(content, list):
            msg.content = content + [{"type": "text", "text": "\n\n" + text}]

    def _find_last_human_message(self, messages: list) -> Any | None:
        """Find the last human message in the list."""
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "human":
                return msg
        return None

    def _inject_directives(self, messages: list, state: dict) -> None:
        """Inject memory directives into messages."""
        human_msg = self._find_last_human_message(messages)
        if human_msg and not self._message_contains(human_msg, "memory-reminder"):
            self._append_to_message(human_msg, MEMORY_REMINDER_DIRECTIVE)

        if state.get("_memory_flush_turn") and messages:
            if not self._message_contains(messages[-1], "memory-flush"):
                logger.info("[MemoryMiddleware] injecting flush directive into last message")
                self._append_to_message(messages[-1], self._get_flush_directive())

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject memory directives before model call."""
        self._inject_directives(request.messages, request.state)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject memory directives before model call."""
        self._inject_directives(request.messages, request.state)
        return await handler(request)

    # ------------------------------------------------------------------
    # Transcript persistence (after_agent)
    # ------------------------------------------------------------------

    def _write_transcript(self, state: MemoryState, runtime: Runtime) -> None:
        """Write full conversation transcript to volume."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            logger.debug("[Transcript] no sandbox available, skipping")
            return

        session_id = state.get("session_id")
        if not session_id:
            logger.debug("[Transcript] no session_id, skipping")
            return

        messages = state.get("messages", [])
        if not messages:
            return

        conversation_text = _extract_full_conversation(messages)
        if not conversation_text:
            return

        started = _first_human_timestamp(messages)
        content = _build_transcript_content(session_id, conversation_text, started)

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            _write_transcript_to_volume(sandbox, session_id, content)
            logger.info("[Transcript] wrote transcript for session %s", session_id)
        except Exception as e:
            logger.warning("[Transcript] failed for session %s: %s", session_id, e)

    def after_agent(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Write session transcript after agent completes."""
        self._write_transcript(state, runtime)
        return None

    async def aafter_agent(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        self._write_transcript(state, runtime)
        return None

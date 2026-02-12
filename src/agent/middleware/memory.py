"""Memory middleware for session archiving, daily log reminders, and pre-compaction flush.

Three responsibilities:
1. Session archive — on new thread start, archives the previous thread's conversation
   to /default-user/memory/YYYY-MM-DD-slug.md (like OpenClaw's session-memory hook).
2. Always-on memory reminder — appended to every user message, nudging the
   agent to follow Memory section instructions (read logs, write when appropriate).
3. Pre-compaction flush — when token count nears the summarization threshold,
   injects a directive to write durable memories before context is compressed.

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
    thread_id: str,
    thread_title: str,
    conversation_text: str,
) -> str:
    """Build the archive markdown file content."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""# Session: {now}

- **Thread ID**: {thread_id}
- **Title**: {thread_title}

## Conversation Summary

{conversation_text}
"""


async def _afind_previous_thread(current_thread_id: str, api_url: str) -> dict | None:
    """Find the most recent main session thread that isn't the current one and hasn't been archived."""
    try:
        url = f"{api_url}/threads/search"
        payload = {
            "metadata": {"graph_id": "task_agent"},
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
        threads = response.json()

        for thread in threads:
            tid = thread.get("thread_id", "")
            values = thread.get("values") or {}

            if tid == current_thread_id:
                continue
            if values.get("_session_archived"):
                continue
            if not values.get("messages"):
                continue

            return thread

    except Exception as e:
        logger.warning("[SessionArchive] failed to search threads: %s", e)

    return None


async def _amark_thread_archived(thread_id: str, api_url: str) -> None:
    """Set _session_archived=true on a thread via LangGraph API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{api_url}/threads/{thread_id}/state",
                headers={"Content-Type": "application/json"},
                json={"values": {"_session_archived": True}},
                timeout=30,
            )
        response.raise_for_status()
    except Exception as e:
        logger.warning("[SessionArchive] failed to mark thread %s archived: %s", thread_id, e)


def _write_archive_to_volume(
    sandbox: modal.Sandbox,
    filename: str,
    content: str,
) -> None:
    """Write archive file to /default-user/memory/ via sandbox."""
    filepath = f"/default-user/memory/{filename}"

    safe_content = content.replace("\\", "\\\\").replace("'", "'\\''")
    cmd = f"cat > '{filepath}' << 'ARCHIVE_EOF'\n{safe_content}\nARCHIVE_EOF"

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
    """Middleware for memory management — session archiving, reminders, and pre-compaction flush.

    before_agent: Archives the previous thread's conversation to a dated markdown file.
    before_model: Tracks token count for pre-compaction flush.
    wrap_model_call: Injects memory reminder and flush directives into messages.
    """

    state_schema = MemoryState

    def __init__(
        self,
        llm: Any = None,
        api_url: str | None = None,
        summarization_threshold: int = 170_000,
        soft_margin: int = 8_000,
        archive_message_limit: int = 15,
    ):
        super().__init__()
        self._llm = llm
        self._api_url = api_url or LANGGRAPH_API_URL
        self._summarization_threshold = summarization_threshold
        self._soft_margin = soft_margin
        self._flush_threshold = summarization_threshold - soft_margin
        self._reset_threshold = summarization_threshold // 2
        self._archive_message_limit = archive_message_limit

    # ------------------------------------------------------------------
    # Session archive (before_agent)
    # ------------------------------------------------------------------

    async def _aarchive_previous_session(self, state: MemoryState) -> None:
        """Archive the previous main session's conversation if not yet archived."""
        if state.get("session_type", "main") != "main":
            return

        current_thread_id = state.get("thread_id")
        sandbox_id = state.get("modal_sandbox_id")
        if not current_thread_id or not sandbox_id or not self._llm:
            return

        prev_thread = await _afind_previous_thread(current_thread_id, self._api_url)
        if not prev_thread:
            return

        prev_thread_id = prev_thread["thread_id"]
        values = prev_thread.get("values") or {}
        prev_title = values.get("thread_title", "Untitled")
        messages = values.get("messages", [])

        conversation_text = _extract_conversation_text(
            messages, limit=self._archive_message_limit
        )
        if not conversation_text:
            await _amark_thread_archived(prev_thread_id, self._api_url)
            return

        slug = _generate_slug(self._llm, conversation_text)

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{date_str}-{slug}.md"
        content = _build_archive_content(prev_thread_id, prev_title, conversation_text)

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            _write_archive_to_volume(sandbox, filename, content)
        except Exception as e:
            logger.warning("[SessionArchive] failed to write archive: %s", e)
            return

        await _amark_thread_archived(prev_thread_id, self._api_url)

    def before_agent(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Default session_type (sync path — archive requires async)."""
        if not state.get("session_type"):
            return {"session_type": "main"}
        return None

    async def abefore_agent(
        self, state: MemoryState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Default session_type, archive previous session, and sync memory index."""
        updates: dict[str, Any] = {}

        if not state.get("session_type"):
            updates["session_type"] = "main"

        try:
            await self._aarchive_previous_session(state)
        except Exception as e:
            logger.warning("[SessionArchive] unexpected error: %s", e)

        # Fire-and-forget memory-index sync — runs in a background thread
        # so it doesn't add ~10s latency to agent startup. If memory_search
        # is called before sync finishes, it searches the stale index (which
        # has everything from prior sessions, just not the just-archived one).
        sandbox_id = state.get("modal_sandbox_id")
        if sandbox_id:
            import threading
            from agent.memory.indexer import sync_memory_index

            def _bg_sync():
                try:
                    import time as _time
                    logger.info("[MemoryIndex] background sync starting (sandbox=%s)", sandbox_id)
                    t0 = _time.monotonic()
                    sb = modal.Sandbox.from_id(sandbox_id)
                    sync_memory_index(sb)
                    logger.info("[MemoryIndex] background sync completed in %.1fs", _time.monotonic() - t0)
                except Exception as e:
                    logger.warning("[MemoryIndex] background sync failed: %s", e)

            threading.Thread(target=_bg_sync, daemon=True).start()

        return updates or None

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

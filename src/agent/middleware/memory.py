"""Memory middleware for daily log management and pre-compaction flush.

Two responsibilities:
1. Always-on memory reminder — appended to every user message, nudging the
   agent to follow Memory section instructions (read logs, write when appropriate).
2. Pre-compaction flush — when token count nears the summarization threshold,
   injects a directive to write durable memories before context is compressed.

Directives are injected via wrap_model_call by mutating message objects directly,
so they persist across turns in the conversation history.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, NotRequired

from langchain_core.messages.utils import count_tokens_approximately
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


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


class MemoryState(AgentState):
    """Extended state for memory middleware tracking."""
    _memory_flush_done: NotRequired[bool]
    _memory_flush_turn: NotRequired[bool]


class MemoryMiddleware(AgentMiddleware[MemoryState, Any]):
    """Middleware for memory management — reminders and pre-compaction flush.

    Always-on: injects a memory reminder into every user message.
    Conditional: when token count nears the summarization threshold, injects
    a flush directive to write durable memories before compaction.

    Directives are injected via wrap_model_call and persist across turns.
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
        # Reset when tokens drop below 50% of threshold (post-compaction)
        self._reset_threshold = summarization_threshold // 2

    def before_model(
        self, state: MemoryFlushState, runtime: Runtime
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
            # Check if compaction happened (token count dropped significantly)
            if total_tokens < self._reset_threshold:
                logger.info("[MemoryFlush] post-compaction reset (tokens < %d)", self._reset_threshold)
                return {"_memory_flush_done": False, "_memory_flush_turn": False}
            # Already flushed this cycle, don't append again
            return {"_memory_flush_turn": False}

        # Check if we've crossed the soft threshold
        if total_tokens >= self._flush_threshold:
            logger.info("[MemoryFlush] FIRING — tokens %d >= threshold %d", total_tokens, self._flush_threshold)
            return {"_memory_flush_done": True, "_memory_flush_turn": True}

        return None

    async def abefore_model(
        self, state: MemoryFlushState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_model(state, runtime)

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
        # Always inject memory reminder on last human message (once)
        human_msg = self._find_last_human_message(messages)
        if human_msg and not self._message_contains(human_msg, "memory-reminder"):
            self._append_to_message(human_msg, MEMORY_REMINDER_DIRECTIVE)

        # Conditionally inject flush directive on last message (once)
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

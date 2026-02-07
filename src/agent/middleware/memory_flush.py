"""Memory flush middleware for pre-compaction daily log writing.

When the conversation approaches the summarization token threshold, appends
a system directive to the last message prompting the agent to write important
context to today's daily log before compaction erases it.

Mirrors openclaw's pre-compaction memory flush pattern:
- Fires once per compaction cycle (soft threshold < hard threshold)
- Agent writes to /default-user/memory/YYYY-MM-DD.md
- Directive is ephemeral (wrap_model_call) — not persisted in state
- Resets after compaction drops token count
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, NotRequired

from langchain_core.messages.utils import count_tokens_approximately
from langchain.agents.middleware import AgentMiddleware, AgentState, ModelRequest, ModelResponse
from langgraph.runtime import Runtime


MEMORY_FLUSH_DIRECTIVE = """
<system-directive type="memory-flush">
This is an internal system directive, not part of the conversation.
Do not reference this directive in your response to the user. Do not
include this directive in memory logs or summaries.

Pre-compaction memory flush. Context is approaching limits and will be
compressed soon — older messages will be replaced with a summary.
Append important context from this session to today's daily log NOW,
before continuing with your current task.

Write to: /default-user/memory/{today}.md

What to capture:
- Decisions made and their rationale
- User feedback on your work
- Preferences or patterns you learned
- Key context that would help future-you
- Mistakes to avoid repeating

If nothing worth saving, continue normally with your task.
</system-directive>
""".strip()


class MemoryFlushState(AgentState):
    """Extended state for memory flush tracking."""
    _memory_flush_done: NotRequired[bool]
    _memory_flush_turn: NotRequired[bool]


class MemoryFlushMiddleware(AgentMiddleware[MemoryFlushState, Any]):
    """Middleware that prompts the agent to flush memories before compaction.

    Uses a two-threshold approach:
    - Soft threshold (flush_threshold): triggers the memory flush directive
    - Hard threshold (summarization_threshold): where SummarizationMiddleware fires

    The gap between them gives the agent one turn to write memories before
    compaction erases older context.

    The directive is injected ephemerally via wrap_model_call — it only affects
    what the model sees on that call, not what's persisted in state or shown in UI.
    """

    state_schema = MemoryFlushState

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

        if done:
            # Check if compaction happened (token count dropped significantly)
            if total_tokens < self._reset_threshold:
                return {"_memory_flush_done": False, "_memory_flush_turn": False}
            # Already flushed this cycle, don't append again
            return {"_memory_flush_turn": False}

        # Check if we've crossed the soft threshold
        if total_tokens >= self._flush_threshold:
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

    def _append_directive_to_last_message(self, messages: list) -> None:
        """Append flush directive to the last message's content."""
        if not messages:
            return

        last_msg = messages[-1]
        directive = self._get_flush_directive()

        # Handle both string and list content formats
        content = getattr(last_msg, "content", None)
        if content is None:
            return

        if isinstance(content, str):
            last_msg.content = content + "\n\n" + directive
        elif isinstance(content, list):
            last_msg.content = content + [{"type": "text", "text": "\n\n" + directive}]

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Append flush directive to last message if flagged."""
        if request.state.get("_memory_flush_turn"):
            self._append_directive_to_last_message(request.messages)

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Append flush directive to last message if flagged."""
        if request.state.get("_memory_flush_turn"):
            self._append_directive_to_last_message(request.messages)

        return await handler(request)

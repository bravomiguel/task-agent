"""Runtime context middleware for injecting runtime context into prompts and messages."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from agent.middleware.modal_sandbox import ModalSandboxState


class RuntimeContextMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that injects runtime context into system prompt and human messages.

    System prompt: thread ID, access tokens, current date/time.
    Human messages: stamps each with a <current-datetime> tag (persistent, once per message).
    """

    state_schema = ModalSandboxState

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

    def _inject_datetime_tag(self, messages: list) -> None:
        """Stamp the last human message with a <current-datetime> tag if not already present."""
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "human":
                if not self._message_contains(msg, "current-datetime"):
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    tag = f'<current-datetime>{now}</current-datetime>'
                    self._append_to_message(msg, tag)
                return

    def _inject_system_prompt_context(self, request: ModelRequest) -> None:
        """Inject runtime context into system prompt."""
        if not request.system_prompt:
            return

        context = ""

        # Current date/time
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        context += f"\n\n### Current Date & Time\n{now}"

        # Thread context
        thread_id = request.state.get("thread_id")
        if thread_id:
            context += (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/default-user/thread-files/{thread_id}/outputs/`."
            )

        # Google Drive access token
        gdrive_token = request.state.get("gdrive_access_token")
        if gdrive_token:
            context += (
                f"\n\n### Google Drive Access Token\n"
                f"For Google Drive API requests, use this access token:\n"
                f"```\n{gdrive_token}\n```"
            )

        if context:
            request.system_prompt = request.system_prompt + context

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject runtime context into system prompt and stamp human messages."""
        self._inject_system_prompt_context(request)
        self._inject_datetime_tag(request.messages)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject runtime context into system prompt and stamp human messages."""
        self._inject_system_prompt_context(request)
        self._inject_datetime_tag(request.messages)
        return await handler(request)

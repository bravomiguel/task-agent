"""Dynamic context middleware for injecting runtime context into prompts."""

from __future__ import annotations

from typing import Any, Callable, Awaitable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from agent.middleware.modal_sandbox import ModalSandboxState


class DynamicContextMiddleware(AgentMiddleware[ModalSandboxState, Any]):
    """Middleware that injects dynamic context (thread ID, access tokens) into system prompt."""

    state_schema = ModalSandboxState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject dynamic context into system prompt."""
        if not request.system_prompt:
            return handler(request)

        dynamic_context = ""

        # Add thread context
        thread_id = request.state.get("thread_id")
        if thread_id:
            dynamic_context += (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/outputs/`."
            )

        # Add Google Drive access token if available
        gdrive_token = request.state.get("gdrive_access_token")
        if gdrive_token:
            dynamic_context += (
                f"\n\n### Google Drive Access Token\n"
                f"For Google Drive API requests, use this access token:\n"
                f"```\n{gdrive_token}\n```"
            )

        if dynamic_context:
            request.system_prompt = request.system_prompt + dynamic_context

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Inject dynamic context into system prompt."""
        if not request.system_prompt:
            return await handler(request)

        dynamic_context = ""

        # Add thread context
        thread_id = request.state.get("thread_id")
        if thread_id:
            dynamic_context += (
                f"\n\n### Current Thread\n"
                f"Your thread ID is `{thread_id}`. "
                f"Save user-requested files to `/threads/{thread_id}/outputs/`."
            )

        # Add Google Drive access token if available
        gdrive_token = request.state.get("gdrive_access_token")
        if gdrive_token:
            dynamic_context += (
                f"\n\n### Google Drive Access Token\n"
                f"For Google Drive API requests, use this access token:\n"
                f"```\n{gdrive_token}\n```"
            )

        if dynamic_context:
            request.system_prompt = request.system_prompt + dynamic_context

        return await handler(request)

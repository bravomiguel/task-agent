"""graph.py - deep agent with Modal sandbox and memory middleware."""

from deepagents.middleware import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.todo import TodoListMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from deepagents_cli.tools import web_search, tavily_client
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_anthropic import ChatAnthropic
import anthropic
from functools import cached_property
from agent.claude_auth import get_claude_code_token
from agent.tools import present_file, view_image, memory_search, sessions_list, sessions_send, sessions_spawn, sessions_history, manage_crons, manage_config, send_message
from agent.web_fetch import web_fetch
from agent.middleware import (
    ConfigMiddleware,
    MemoryMiddleware,
    ModalSandboxMiddleware,
    MoveUploadsMiddleware,
    SessionSetupMiddleware,
    RuntimeContextMiddleware,
    SessionMetadataMiddleware,
    ToolDescriptionMiddleware,
)
from agent.system_prompt import STATIC_PART_01
from agent.modal_backend import LazyModalBackend

import os
os.environ.pop("ANTHROPIC_API_KEY", None)

OAUTH_BETAS = "claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14,interleaved-thinking-2025-05-14"


class _OAuthChatAnthropic(ChatAnthropic):
    """ChatAnthropic with Claude Code OAuth token auth.

    Uses auth_token (Bearer) instead of api_key (x-api-key).
    Flattens system to string (OAuth endpoint rejects list format).
    No prompt caching — incompatible with OAuth.
    """

    @cached_property
    def _client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(
            auth_token=get_claude_code_token(),
            max_retries=self.max_retries,
            default_headers={"anthropic-beta": OAUTH_BETAS},
        )

    @cached_property
    def _async_client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            auth_token=get_claude_code_token(),
            max_retries=self.max_retries,
            default_headers={"anthropic-beta": OAUTH_BETAS},
        )

    @staticmethod
    def _flatten_system(payload: dict) -> dict:
        system = payload.get("system")
        if isinstance(system, list):
            parts = [b["text"] for b in system if isinstance(b, dict) and b.get("type") == "text"]
            payload["system"] = "\n\n".join(parts) if parts else payload.pop("system", None)
        return payload

    def _create(self, payload: dict) -> anthropic.types.Message:
        return self._client.messages.create(**self._flatten_system(payload))

    async def _acreate(self, payload: dict) -> anthropic.types.Message:
        return await self._async_client.messages.create(**self._flatten_system(payload))


# Initialize models — Claude Sonnet 4.6 via OAuth token
main_model = _OAuthChatAnthropic(
    model="claude-sonnet-4-6",
    anthropic_api_key="unused",
)
gpt_4_1_mini = init_chat_model(model="openai:gpt-4.1-mini", disable_streaming=True)


def create_backend_factory():
    """Create a backend factory that builds LazyModalBackend from runtime state."""

    def backend_factory(runtime):
        # LazyModalBackend handles all paths under /mnt/
        # User volume is mounted by ModalSandboxMiddleware
        return LazyModalBackend(runtime)

    return backend_factory


# Create a single instance of ModalSandboxMiddleware to be shared
modal_sandbox_middleware = ModalSandboxMiddleware()


agent_middleware = [
    modal_sandbox_middleware,
    MoveUploadsMiddleware(),
    SessionSetupMiddleware(llm=gpt_4_1_mini),  # Parallel: prompt files + skills + memory setup
    ConfigMiddleware(),  # Load config, heartbeat management (active hours, cron reconcile, early exit)
    RuntimeContextMiddleware(),  # Assemble: STATIC_PART_01 → Skills → STATIC_PART_02 → Session → Project Context → STATIC_PART_03
    ToolDescriptionMiddleware(),
    MemoryMiddleware(),  # Memory reminders + pre-compaction flush
    SessionMetadataMiddleware(),
]

# Build tools list - conditionally include web_search if Tavily is available
tools = [web_fetch, present_file, view_image, memory_search, manage_config, manage_crons, send_message, sessions_list, sessions_history, sessions_send, sessions_spawn]
if tavily_client is not None:
    tools.append(web_search)

backend = create_backend_factory()

deepagent_middleware = [
    TodoListMiddleware(system_prompt="."),
    FilesystemMiddleware(backend=backend, system_prompt=""),
    SummarizationMiddleware(
        model=main_model,
        max_tokens_before_summary=170000,
        messages_to_keep=6,
    ),
    # AnthropicPromptCachingMiddleware disabled — converts system to list format
    # which OAuth endpoint rejects. Prompt caching not supported with OAuth tokens.
    PatchToolCallsMiddleware(),
    # Custom middleware (appended after deepagents defaults)
    *agent_middleware,
]

main = create_agent(
    main_model,
    system_prompt=STATIC_PART_01,
    tools=tools,
    middleware=deepagent_middleware,
)

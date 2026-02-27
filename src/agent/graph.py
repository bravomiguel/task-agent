"""graph.py - deep agent with Modal sandbox and memory middleware."""

from deepagents import create_deep_agent
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain.chat_models import init_chat_model
from langchain_anthropic import ChatAnthropic
from agent.claude_auth import get_claude_code_token
from agent.tools import present_file, view_image, memory_search
from agent.cron_tools import manage_crons
from agent.middleware import (
    HeartbeatMiddleware,
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

# Initialize models — use Claude Code OAuth token for auth.
# Clear ANTHROPIC_API_KEY so the SDK doesn't send X-Api-Key alongside Bearer.
# The oauth-2025-04-20 beta header is required for OAuth token auth.
import os
os.environ.pop("ANTHROPIC_API_KEY", None)
_claude_token = get_claude_code_token()
claude_opus = ChatAnthropic(
    model="claude-opus-4-6",
    default_headers={
        "Authorization": f"Bearer {_claude_token}",
        "anthropic-beta": "oauth-2025-04-20",
    },
)
gpt_4_1_mini = init_chat_model(model="openai:gpt-4.1-mini", disable_streaming=True)


def create_backend_factory():
    """Create a backend factory that builds LazyModalBackend from runtime state."""

    def backend_factory(runtime):
        # LazyModalBackend handles all paths under /default-user/
        # User volume is mounted by ModalSandboxMiddleware
        return LazyModalBackend(runtime)

    return backend_factory


# Create a single instance of ModalSandboxMiddleware to be shared
modal_sandbox_middleware = ModalSandboxMiddleware()


agent_middleware = [
    modal_sandbox_middleware,
    MoveUploadsMiddleware(),
    HeartbeatMiddleware(),  # Heartbeat detection + early exit
    SessionSetupMiddleware(llm=gpt_4_1_mini),  # Parallel: prompt files + skills + memory setup
    RuntimeContextMiddleware(),  # Assemble: STATIC_PART_01 → Skills → STATIC_PART_02 → Session → Project Context → STATIC_PART_03
    ToolDescriptionMiddleware(),
    MemoryMiddleware(),  # Memory reminders + pre-compaction flush
    SessionMetadataMiddleware(),
]

# Build tools list - conditionally include web_search if Tavily is available
tools = [http_request, fetch_url, present_file, view_image, memory_search, manage_crons]
if tavily_client is not None:
    tools.append(web_search)

# Create the agent with backend factory
agent = create_deep_agent(
    model=claude_opus,
    system_prompt=STATIC_PART_01,
    tools=tools,
    middleware=agent_middleware,
    backend=create_backend_factory(),
)

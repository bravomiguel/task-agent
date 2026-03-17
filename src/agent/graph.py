"""graph.py - deep agent with Modal sandbox and memory middleware."""

from deepagents.middleware import FilesystemMiddleware
from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware
from langchain.agents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware.todo import TodoListMiddleware
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from deepagents_cli.tools import web_search, tavily_client
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
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

# Initialize models — configurable at runtime via RunnableConfig.
# For prod: switch to "anthropic:claude-sonnet-4-6"
# Override at runtime: config={"configurable": {"model": "anthropic:claude-sonnet-4-6"}}
main_model = init_chat_model(
    model="openai:gpt-5.4",
    configurable_fields=["model", "model_provider"],
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
    AnthropicPromptCachingMiddleware(unsupported_model_behavior="ignore"),
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

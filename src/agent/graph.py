"""graph.py - deep agent with Modal sandbox and memory middleware."""

from deepagents import create_deep_agent
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain.chat_models import init_chat_model
from langchain_openai import ChatOpenAI
from agent.tools import present_file, view_image
from agent.memory.tools import memory_search
from agent.middleware import (
    AgentsPromptMiddleware,
    MemoryMiddleware,
    ModalSandboxMiddleware,
    MoveUploadsMiddleware,
    RuntimeContextMiddleware,
    ThreadTitleMiddleware,
    IsDoneMiddleware,
    OpenFilePathMiddleware,
    ToolDescriptionMiddleware,
    SkillsMiddleware,
)
from agent.system_prompt import SYSTEM_PROMPT
from agent.modal_backend import LazyModalBackend

# Initialize models
gpt_5_1 = ChatOpenAI(
    model="gpt-5.1",
    reasoning_effort="low",
    use_responses_api=True,
    output_version="responses/v1",
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
    MoveUploadsMiddleware(),  # Move temp uploads before agent runs
    AgentsPromptMiddleware(),  # Load AGENTS.md from volume into prompt
    RuntimeContextMiddleware(),
    SkillsMiddleware(),
    ToolDescriptionMiddleware(),
    MemoryMiddleware(llm=gpt_4_1_mini),  # Session archive + memory reminders + pre-compaction flush
    IsDoneMiddleware(),
    OpenFilePathMiddleware(),
    ThreadTitleMiddleware(llm=gpt_4_1_mini),
]

# Build tools list - conditionally include web_search if Tavily is available
tools = [http_request, fetch_url, present_file, view_image, memory_search]
if tavily_client is not None:
    tools.append(web_search)

# Create the agent with backend factory
agent = create_deep_agent(
    model=gpt_5_1,
    system_prompt=SYSTEM_PROMPT,
    tools=tools,
    middleware=agent_middleware,
    backend=create_backend_factory(),
)

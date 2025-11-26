"""graph.py - deep agent with Modal sandbox and memory middleware."""

from deepagents import create_deep_agent
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain.chat_models import init_chat_model
from agent.middleware import ModalSandboxMiddleware, DynamicContextMiddleware, ReviewMessageMiddleware, ThreadTitleMiddleware, IsDoneMiddleware, DateTimeContextMiddleware
from agent.system_prompt import SYSTEM_PROMPT
from agent.modal_backend import LazyModalBackend

# Initialize model
gpt_4_1 = init_chat_model(model="openai:gpt-4.1")
gpt_4_1_mini = init_chat_model(model="openai:gpt-4.1-mini", disable_streaming=True)


def create_backend_factory():
    """Create a backend factory that builds LazyModalBackend from runtime state."""

    def backend_factory(runtime):
        # LazyModalBackend handles all paths: /threads, /memories, /workspace
        # Volumes are mounted by ModalSandboxMiddleware
        return LazyModalBackend(runtime)

    return backend_factory


# Create a single instance of ModalSandboxMiddleware to be shared
modal_sandbox_middleware = ModalSandboxMiddleware(idle_timeout=30)


agent_middleware = [
    modal_sandbox_middleware,
    DynamicContextMiddleware(),
    DateTimeContextMiddleware(),
    IsDoneMiddleware(),
    ThreadTitleMiddleware(llm=gpt_4_1_mini),
    ReviewMessageMiddleware(llm=gpt_4_1_mini),
]

# Build tools list - conditionally include web_search if Tavily is available
tools = [http_request, fetch_url]
if tavily_client is not None:
    tools.append(web_search)

# Create the agent with backend factory
agent = create_deep_agent(
    model=gpt_4_1,
    system_prompt=SYSTEM_PROMPT,
    tools=tools,
    middleware=agent_middleware,
    backend=create_backend_factory(),
)

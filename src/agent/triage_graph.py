"""triage_graph.py - lightweight triage agent for filtering incoming events."""

from deepagents import create_deep_agent
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain_openai import ChatOpenAI
from agent.middleware import ModalSandboxMiddleware
from agent.triage_prompt import TRIAGE_SYSTEM_PROMPT
from agent.modal_backend import LazyModalBackend

# Initialize model - GPT-4.1 for better reasoning
gpt_4_1 = ChatOpenAI(model="gpt-4.1")


def create_backend_factory():
    """Create a backend factory that builds LazyModalBackend from runtime state."""

    def backend_factory(runtime):
        # LazyModalBackend handles all paths including /memories
        return LazyModalBackend(runtime)

    return backend_factory


# Create a single instance of ModalSandboxMiddleware for triage agent
# Only mount memories volume, no threads or skills needed
triage_sandbox_middleware = ModalSandboxMiddleware(
    idle_timeout=60,  # 1 minute idle timeout for quick cleanup
    memory_volume_name="memories",  # Only memories needed
    skills_volume_name="skills",  # Still need skills volume for compatibility
)

# Minimal middleware - no file operations, no titles, no reviews
triage_middleware = [
    triage_sandbox_middleware,
]

# Build tools list - same as main agent
tools = [http_request, fetch_url]
if tavily_client is not None:
    tools.append(web_search)

# Create the triage agent with backend factory
triage_agent = create_deep_agent(
    model=gpt_4_1,
    system_prompt=TRIAGE_SYSTEM_PROMPT,
    tools=tools,
    middleware=triage_middleware,
    backend=create_backend_factory(),
)

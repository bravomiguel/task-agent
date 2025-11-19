"""graph.py - deep agent with Modal sandbox and memory middleware."""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents_cli.config import get_default_coding_instructions
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain.chat_models import init_chat_model
from agent.middleware import AsyncAgentMemoryMiddleware, ModalSandboxMiddleware, ThreadContextMiddleware, ReviewMessageMiddleware, ThreadTitleMiddleware, IsDoneMiddleware
from agent.system_prompt import SYSTEM_PROMPT
from agent.modal_backend import LazyModalBackend
from deepagents.backends import CompositeBackend

# Initialize model
gpt_4_1 = init_chat_model(model="openai:gpt-4.1")
gpt_5_mini = init_chat_model(model="openai:gpt-5-mini", disable_streaming=True)

# Initialize agent directory and agent.md file
assistant_id = "my-agent"
agent_dir = Path.home() / ".deepagents" / assistant_id
agent_dir.mkdir(parents=True, exist_ok=True)
agent_md = agent_dir / "agent.md"
if not agent_md.exists():
    agent_md.write_text(get_default_coding_instructions())

# The agent.md file is stored locally
long_term_backend = FilesystemBackend(root_dir=agent_dir, virtual_mode=True)


def create_backend_factory(filesystem_backend: FilesystemBackend):
    """Create a backend factory that builds CompositeBackend with LazyModalBackend from runtime state."""

    def backend_factory(runtime) -> CompositeBackend:
        # Use lazy backend that defers sandbox connection until actual use
        # This allows FilesystemMiddleware to check for execution support
        # without needing state access
        lazy_modal_backend = LazyModalBackend(runtime)

        return CompositeBackend(
            default=lazy_modal_backend,
            routes={"/memories/": filesystem_backend}
        )

    return backend_factory


# Create a single instance of ModalSandboxMiddleware to be shared
modal_sandbox_middleware = ModalSandboxMiddleware(idle_timeout=60)


agent_middleware = [
    modal_sandbox_middleware,
    ThreadContextMiddleware(),
    IsDoneMiddleware(),
    ThreadTitleMiddleware(llm=gpt_5_mini),
    AsyncAgentMemoryMiddleware(
        backend=long_term_backend,
        memory_path="/memories/"
    ),
    ReviewMessageMiddleware(llm=gpt_5_mini),
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
    backend=create_backend_factory(long_term_backend),
)

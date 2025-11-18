"""graph.py - deep agent with Modal sandbox and memory middleware."""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents_cli.agent_memory import AgentMemoryMiddleware
from deepagents_cli.config import get_default_coding_instructions
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from deepagents_cli.integrations.modal import ModalBackend
from langchain.chat_models import init_chat_model
from langgraph.prebuilt.tool_node import ToolRuntime
from agent.middleware import AsyncAgentMemoryMiddleware, ModalSandboxMiddleware, ReviewMessageMiddleware, ThreadTitleMiddleware, IsDoneMiddleware

# System prompt for Modal sandbox mode
system_prompt = """### Current Working Directory      
      
You are operating in a **remote Linux sandbox** at `/workspace`.      
      
All code execution and file operations happen in this sandbox environment.      
      
**Important:**      
- The CLI is running locally on the user's machine, but you execute code remotely      
- Use `/workspace` as your working directory for all operations      
- The local `/memories/` directory is still accessible for persistent storage      
      
### Memory System Reminder      
      
Your long-term memory is stored in /memories/ and persists across sessions.      
      
**IMPORTANT - Check memories before answering:**      
- When asked "what do you know about X?" → Run `ls /memories/` FIRST, then read relevant files      
- When starting a task → Check if you have guides or examples in /memories/      
- At the beginning of new sessions → Consider checking `ls /memories/` to see what context you have      
      
Base your answers on saved knowledge (from /memories/) when available, supplemented by general knowledge.      
      
### Human-in-the-Loop Tool Approval      
      
Some tool calls require user approval before execution. When a tool call is rejected by the user:      
1. Accept their decision immediately - do NOT retry the same command      
2. Explain that you understand they rejected the action      
3. Suggest an alternative approach or ask for clarification      
4. Never attempt the exact same rejected command again      
      
Respect the user's decisions and work with them collaboratively.      
      
### Web Search Tool Usage      
      
When you use the web_search tool:      
1. The tool will return search results with titles, URLs, and content excerpts      
2. You MUST read and process these results, then respond naturally to the user      
3. NEVER show raw JSON or tool results directly to the user      
4. Synthesize the information from multiple sources into a coherent answer      
5. Cite your sources by mentioning page titles or URLs when relevant      
6. If the search doesn't find what you need, explain what you found and ask clarifying questions      
      
The user only sees your text responses - not tool results. Always provide a complete, natural language answer after using web_search.      
      
### Todo List Management      
      
When using the write_todos tool:      
1. Keep the todo list MINIMAL - aim for 3-6 items maximum      
2. Only create todos for complex, multi-step tasks that truly need tracking      
3. Break down work into clear, actionable items without over-fragmenting      
4. For simple tasks (1-2 steps), just do them directly without creating todos      
5. When first creating a todo list for a task, ALWAYS ask the user if the plan looks good before starting work      
   - Create the todos, let them render, then ask: "Does this plan look good?" or similar      
   - Wait for the user's response before marking the first todo as in_progress      
   - If they want changes, adjust the plan accordingly      
6. Update todo status promptly as you complete each item      
      
The todo list is a planning tool - use it judiciously to avoid overwhelming the user with excessive task tracking."""

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

# The agent.md file is stored locally, not in the store
long_term_backend = FilesystemBackend(root_dir=agent_dir, virtual_mode=True)

# Create a single instance of ModalSandboxMiddleware to be shared
modal_sandbox_middleware = ModalSandboxMiddleware(idle_timeout=30)


class _LazyModalBackend:
    """A lazy wrapper that defers ModalBackend creation until first use.

    This is needed because FilesystemMiddleware calls the backend factory
    from wrap_model_call with Runtime (no state), but we need state to get
    sandbox_id. The actual backend is created lazily when methods are called
    from tool context (ToolRuntime with state).
    """

    def __init__(self, runtime):
        self._runtime = runtime
        self._backend = None

    def _get_backend(self):
        if self._backend is None:
            import modal
            if not hasattr(self._runtime, 'state'):
                raise RuntimeError("Cannot access backend - no state available in runtime context")
            sandbox_id = self._runtime.state.get("modal_sandbox_id")
            if not sandbox_id:
                raise RuntimeError("Modal sandbox not initialized")
            sandbox = modal.Sandbox.from_id(sandbox_id)
            self._backend = ModalBackend(sandbox)
        return self._backend

    # Implement SandboxBackendProtocol methods by delegation
    def ls_info(self, path):
        return self._get_backend().ls_info(path)

    def read(self, file_path, offset=0, limit=2000):
        return self._get_backend().read(file_path, offset, limit)

    def write(self, file_path, content):
        return self._get_backend().write(file_path, content)

    def edit(self, file_path, old_string, new_string, replace_all=False):
        return self._get_backend().edit(file_path, old_string, new_string, replace_all)

    def grep_raw(self, pattern, path=None, glob=None):
        return self._get_backend().grep_raw(pattern, path, glob)

    def glob_info(self, pattern, path="/"):
        return self._get_backend().glob_info(pattern, path)

    def execute(self, command):
        return self._get_backend().execute(command)

    @property
    def id(self):
        # Return a placeholder if backend not yet created
        # This allows isinstance() checks to pass without triggering backend creation
        if self._backend is None:
            return "lazy-modal-backend"
        return self._backend.id


def create_backend_factory(filesystem_backend: FilesystemBackend):
    """Create a backend factory that builds CompositeBackend with ModalBackend from runtime state."""

    def backend_factory(runtime) -> CompositeBackend:
        # Use lazy backend that defers sandbox connection until actual use
        # This allows FilesystemMiddleware to check for execution support
        # without needing state access
        lazy_modal_backend = _LazyModalBackend(runtime)

        return CompositeBackend(
            default=lazy_modal_backend,
            routes={"/memories/": filesystem_backend}
        )

    return backend_factory


agent_middleware = [
    modal_sandbox_middleware,
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
    system_prompt=system_prompt,
    tools=tools,
    middleware=agent_middleware,
    backend=create_backend_factory(long_term_backend),
)

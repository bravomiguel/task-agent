"""graph.py - deep agent with Modal sandbox and memory middleware."""

from pathlib import Path
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend
from deepagents.backends.filesystem import FilesystemBackend
from deepagents_cli.agent_memory import AgentMemoryMiddleware
from deepagents_cli.integrations.sandbox_factory import create_modal_sandbox
from deepagents_cli.config import get_default_coding_instructions
from deepagents_cli.tools import http_request, fetch_url, web_search, tavily_client
from langchain.chat_models import init_chat_model
from agent.middleware import AsyncAgentMemoryMiddleware, ReviewMessageMiddleware, ThreadTitleMiddleware, IsDoneMiddleware

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

# Create Modal sandbox backend for remote code execution
modal_sandbox = create_modal_sandbox()

# IMPORTANT: Use FilesystemBackend for AgentMemoryMiddleware
# The agent.md file is stored locally, not in the store
long_term_backend = FilesystemBackend(root_dir=agent_dir, virtual_mode=True)

# Backend: Remote sandbox for code + local /memories/
composite_backend = CompositeBackend(
    default=modal_sandbox,  # Remote sandbox (ModalBackend, etc.)
    # Agent memories (still local!)
    routes={"/memories/": long_term_backend},
)


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

# Middleware: AgentMemoryMiddleware for long-term memory management
agent_middleware = [
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

# Create the agent with InMemoryStore
agent = create_deep_agent(
    model=gpt_4_1,
    system_prompt=system_prompt,
    tools=tools,
    backend=composite_backend,
    middleware=agent_middleware,
)

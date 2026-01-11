"""triage_graph.py - lightweight triage agent for filtering incoming events."""

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from agent.middleware import (
    ModalSandboxMiddleware,
    TriageRulesMiddleware,
    TriageThreadsMiddleware,
    TriageContextMiddleware,
)
from agent.triage_prompt import TRIAGE_SYSTEM_PROMPT
from agent.tools import route_event
from agent.modal_backend import LazyModalBackend

# Initialize model
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

# Triage middleware stack:
# BEFORE_AGENT:
#   1. ModalSandboxMiddleware - creates sandbox, mounts volumes
#   2. TriageRulesMiddleware - reads /memories/triage.md into state
#   3. TriageThreadsMiddleware - fetches threads, dumps active to sandbox
#   4. TriageContextMiddleware - injects rules + thread count into prompt
triage_middleware = [
    triage_sandbox_middleware,
    TriageRulesMiddleware(),
    TriageThreadsMiddleware(),
    TriageContextMiddleware(),
]

# route_event tool uses InjectedState to access messages
# Agent also gets file tools (provided by backend) for searching thread files
tools = [route_event]

# Create the triage agent with backend factory
triage_agent = create_deep_agent(
    model=gpt_4_1,
    system_prompt=TRIAGE_SYSTEM_PROMPT,
    tools=tools,
    middleware=triage_middleware,
    backend=create_backend_factory(),
)

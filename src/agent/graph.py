"""Minimal agent creation with Modal sandbox and memory middleware."""  
  
from deepagents import create_deep_agent  
from deepagents.backends import CompositeBackend, StoreBackend  
from deepagents_cli.agent_memory import AgentMemoryMiddleware  
from deepagents_cli.integrations.sandbox_factory import create_modal_sandbox  
from langchain_core.language_models import BaseChatModel  
from langgraph.checkpoint.memory import InMemorySaver  
from langgraph.store.memory import InMemoryStore  
  
  
def create_minimal_agent(  
    model: str | BaseChatModel,  
    assistant_id: str,  
    tools: list,  
    system_prompt: str,  
):  
    """Create a minimal agent with Modal sandbox and memory middleware.  
      
    Args:  
        model: LLM model to use (e.g., "claude-sonnet-4-20250514")  
        assistant_id: Agent identifier for memory storage  
        tools: Additional tools (e.g., web_search, http_request, fetch_url)  
        system_prompt: System prompt for the agent  
          
    Returns:  
        Configured agent ready for execution  
    """  
    # Create Modal sandbox backend for remote code execution  
    modal_sandbox = create_modal_sandbox()  
      
    # Composite backend factory: Modal sandbox (default) + StoreBackend for /memories/  
    composite_backend = lambda rt: CompositeBackend(  
        default=modal_sandbox,  # ModalBackend instead of StateBackend  
        routes={  
            "/memories/": StoreBackend(rt),  # StoreBackend instantiated with runtime  
        }  
    )  
      
    # Middleware: AgentMemoryMiddleware for long-term memory management  
    agent_middleware = [  
        AgentMemoryMiddleware(  
            backend=(lambda rt: StoreBackend(rt)),  
            memory_path="/memories/"  
        ),  
    ]  
      
    # Create the agent with InMemoryStore  
    agent = create_deep_agent(  
        model=model,  
        system_prompt=system_prompt,  
        tools=tools,  
        backend=composite_backend,  # Factory function, not instance  
        middleware=agent_middleware,  
        store=InMemoryStore(),  # Store passed to create_deep_agent  
    )  
      
    # Add checkpointer for state persistence  
    agent.checkpointer = InMemorySaver()  
      
    return agent
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime
from typing import Annotated, Any, NotRequired
from typing_extensions import TypedDict

# Extend AgentState to include thread_title


class ThreadTitleState(AgentState):
    thread_title: NotRequired[str]


class ThreadTitleMiddleware(AgentMiddleware[ThreadTitleState]):
    """Middleware that generates a thread title from the initial user message."""

    state_schema = ThreadTitleState

    def __init__(self, llm):
        """Initialize with an LLM for title generation."""
        super().__init__()
        self.llm = llm

    def before_agent(
        self, state: ThreadTitleState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Sync version: Generate thread title on first message."""
        # Only run if thread_title doesn't exist (first message)
        if "thread_title" in state and state["thread_title"]:
            return None

        # Get the first user message
        messages = state["messages"]
        first_user_msg = next(
            (m for m in messages if isinstance(m, HumanMessage)), None)

        if not first_user_msg:
            return None

        # Use LLM to generate a short title
        title_prompt = f"Generate a short title for this conversation, do not include double quotes: {first_user_msg.content}"
        title = self.llm.invoke(title_prompt).content

        return {"thread_title": title}

    async def abefore_agent(
        self, state: ThreadTitleState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version: Generate thread title on first message."""
        # Only run if thread_title doesn't exist (first message)
        if "thread_title" in state and state["thread_title"]:
            return None

        # Get the first user message
        messages = state["messages"]
        first_user_msg = next(
            (m for m in messages if isinstance(m, HumanMessage)), None)

        if not first_user_msg:
            return None

        # Use LLM to generate a short title (async)
        title_prompt = f"Generate a short title for this conversation, do not include double quotes: {first_user_msg.content}"
        title = (await self.llm.ainvoke(title_prompt)).content

        return {"thread_title": title}


def is_done_reducer(left, right):
    # If both are None, default to False
    if left is None and right is None:
        return False
    # If only right is None, keep left
    if right is None:
        return left
    # Otherwise, use right (the new value)
    return right


class IsDoneState(AgentState):
    is_done: Annotated[NotRequired[bool], is_done_reducer]


class IsDoneMiddleware(AgentMiddleware[IsDoneState]):
    """Middleware that adds is_done boolean to agent state."""
    state_schema = IsDoneState


class ReviewState(AgentState):
    review_message: NotRequired[str]


class ReviewMessageMiddleware(AgentMiddleware[ReviewState]):  
    state_schema = ReviewState  
      
    def __init__(self, llm):  
        """Initialize with an LLM for title generation."""  
        super().__init__()  
        self.llm = llm  
      
    def after_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:  
        """Sync version: Generate review message after agent completes."""  
        messages = state["messages"]  
          
        if not messages:  
            return {"review_message": "No action needed."}  
          
        summary_prompt = f"""Review this conversation and summarize what input or request is needed from the human user.  
        Be concise and specific about what action or information is required.  
          
        Conversation:  
        {chr(10).join(f"{msg.type}: {msg.content}" for msg in messages[-5:])}  
          
        Provide a brief summary (1 sentence) of what the user needs to do next."""  
          
        response = self.llm.invoke([{"role": "user", "content": summary_prompt}])  
          
        return {"review_message": response.content}  
      
    async def aafter_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:  
        """Async version: Generate review message after agent completes."""  
        messages = state["messages"]  
          
        if not messages:  
            return {"review_message": "No action needed."}  
          
        summary_prompt = f"""Review this conversation and summarize what input or request is needed from the human user.  
        Be concise and specific about what action or information is required.  
          
        Conversation:  
        {chr(10).join(f"{msg.type}: {msg.content}" for msg in messages[-5:])}  
          
        Provide a brief summary (1 sentence) of what the user needs to do next."""  
          
        response = await self.llm.ainvoke([{"role": "user", "content": summary_prompt}])  
          
        return {"review_message": response.content}

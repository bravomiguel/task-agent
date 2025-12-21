"""Thread title middleware for generating conversation titles."""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime


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

"""Review message middleware for generating post-agent summaries."""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime


def _sanitize_content(content: Any) -> str:
    """Sanitize message content by redacting image data."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "image_url":
                    parts.append("[image]")
                elif block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


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

        # Get last 5 messages for context
        recent_messages = messages[-5:]
        messages_text = "\n\n".join([
            f"{msg.__class__.__name__}: {_sanitize_content(msg.content)}"
            for msg in recent_messages
        ])

        summary_prompt = f"""Based on this conversation, what does the user need to do next?

        Recent conversation:
        {messages_text}

        Provide a brief, imperative instruction addressed directly to the user (1 sentence).
        Include enough context to be descriptive but stay concise.
        Examples: "Review the Modal sandbox changes and test the agent." or "Run the updated migration script for the database." or "No action needed."
        Be direct and actionable."""

        response = self.llm.invoke(
            [{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}

    async def aafter_agent(self, state: ReviewState, runtime: Runtime) -> dict[str, Any] | None:
        """Async version: Generate review message after agent completes."""
        messages = state["messages"]

        if not messages:
            return {"review_message": "No action needed."}

        # Get last 5 messages for context
        recent_messages = messages[-5:]
        messages_text = "\n\n".join([
            f"{msg.__class__.__name__}: {_sanitize_content(msg.content)}"
            for msg in recent_messages
        ])

        summary_prompt = f"""Based on this conversation, what does the user need to do next?

        Recent conversation:
        {messages_text}

        Provide a brief, imperative instruction addressed directly to the user (1 sentence).
        Include enough context to be descriptive but stay concise.
        Examples: "Review the Modal sandbox changes and test the agent." or "Run the updated migration script for the database." or "No action needed."
        Be direct and actionable."""

        response = await self.llm.ainvoke([{"role": "user", "content": summary_prompt}])

        return {"review_message": response.content}

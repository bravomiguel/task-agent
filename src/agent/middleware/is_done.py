"""Is done middleware for tracking task completion state."""

from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState


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

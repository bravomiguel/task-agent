"""Session metadata middleware — contributes session_title and is_done state."""

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


class SessionMetadataState(AgentState):
    session_title: NotRequired[str]
    is_done: Annotated[NotRequired[bool], is_done_reducer]


class SessionMetadataMiddleware(AgentMiddleware[SessionMetadataState]):
    """Middleware that adds session_title and is_done to agent state.

    Both fields are set externally (by the frontend or agent output).
    This middleware only contributes the state schema — no hooks.
    """

    state_schema = SessionMetadataState

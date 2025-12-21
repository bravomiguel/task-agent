"""Open file path middleware for tracking currently open file."""

from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState


def open_file_path_reducer(left, right):
    # Right value always takes precedence, including None to clear
    return right


class OpenFilePathState(AgentState):
    open_file_path: Annotated[NotRequired[str | None], open_file_path_reducer]


class OpenFilePathMiddleware(AgentMiddleware[OpenFilePathState]):
    """Middleware that adds open_file_path string to agent state.

    Tracks the single currently open file path, or None if no file is open.
    """
    state_schema = OpenFilePathState

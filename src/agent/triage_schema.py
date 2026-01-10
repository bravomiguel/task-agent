"""Triage schema - Pydantic models for triage agent structured output."""

from typing import Literal

from pydantic import BaseModel, Field


class TriageDecision(BaseModel):
    """Decision for how to handle an incoming event."""

    action: Literal["filter_out", "route"] = Field(
        description="'filter_out' to discard the event, 'route' to send to a thread"
    )
    thread_id: str | None = Field(
        default=None,
        description="Thread to route to: 'new' for new thread, or existing thread UUID. Required if action is 'route'.",
    )

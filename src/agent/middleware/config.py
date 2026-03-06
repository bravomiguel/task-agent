"""Config middleware — gate heartbeat sessions by HEARTBEAT.md content."""

from __future__ import annotations

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)


class ConfigState(AgentState):
    """State schema for config middleware."""

    session_type: NotRequired[str]  # main | cron | subagent
    cron_job_name: NotRequired[str]


class ConfigMiddleware(AgentMiddleware[ConfigState, Any]):
    """Gate heartbeat sessions by HEARTBEAT.md content.

    before_agent:
      - On heartbeat cron sessions: checks if HEARTBEAT.md has actionable
        content, early-exits if empty.
      - All other sessions: no-op.

    Active hours filtering is handled by the cron-launcher edge function
    before a thread is created (timezone + active_hours are baked into the
    heartbeat cron job body).

    Must run AFTER SessionSetupMiddleware (needs prompt_files for HEARTBEAT.md).
    """

    state_schema = ConfigState

    def _is_heartbeat_empty(self, state: dict) -> bool:
        """Check if HEARTBEAT.md is empty or missing using already-loaded prompt_files."""
        prompt_files = state.get("prompt_files") or {}
        content = prompt_files.get("HEARTBEAT.md", "").strip()
        if not content:
            return True
        lines = [
            line
            for line in content.split("\n")
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("<!--")
        ]
        return len(lines) == 0

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self, state: ConfigState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Gate heartbeat sessions by content."""
        if state.get("cron_job_name") != "heartbeat":
            return None

        if self._is_heartbeat_empty(state):
            logger.info("[Config] HEARTBEAT.md empty, early exit")
            return {"jump_to": "end"}

        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: ConfigState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync."""
        return self.before_agent(state, runtime)

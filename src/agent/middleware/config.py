"""Config middleware — gate heartbeat sessions by active hours and content."""

from __future__ import annotations

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

from agent.config import is_within_active_hours, load_config

logger = logging.getLogger(__name__)


class ConfigState(AgentState):
    """State schema for config middleware."""

    session_type: NotRequired[str]  # main | cron | subagent
    cron_job_name: NotRequired[str]


class ConfigMiddleware(AgentMiddleware[ConfigState, Any]):
    """Gate heartbeat sessions by active hours and HEARTBEAT.md content.

    before_agent:
      - On heartbeat cron sessions: loads config, checks active hours and
        HEARTBEAT.md content, early-exits if outside hours or empty.
      - All other sessions: no-op.

    Heartbeat cron creation and config init are handled by the reset/provisioning
    script. Mid-run config changes are handled at their source (manage_config tool
    or dashboard API route).

    Must run AFTER ModalSandboxMiddleware (needs sandbox_id).
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
        """Gate heartbeat sessions by active hours and content."""
        if state.get("cron_job_name") != "heartbeat":
            return None

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            logger.warning("[Config] no sandbox_id, skipping active hours check")
            return None

        config = load_config(sandbox_id)

        if not is_within_active_hours(config):
            logger.info("[Config] heartbeat outside active hours, early exit")
            return {"jump_to": "end"}

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

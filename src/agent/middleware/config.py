"""Config middleware — load user config, heartbeat management on session start."""

from __future__ import annotations

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

from agent.config import (
    UserConfig,
    is_within_active_hours,
    load_config,
    reconcile_heartbeat_cron,
    _parse_every_to_cron,
)

logger = logging.getLogger(__name__)

HEARTBEAT_INPUT_MESSAGE = (
    "Read HEARTBEAT.md from your project context and execute any tasks listed. "
    "Do not infer or repeat old tasks from prior sessions."
)


class ConfigState(AgentState):
    """State schema for config middleware."""

    config: NotRequired[dict]  # serialized UserConfig
    session_type: NotRequired[str]  # main | cron | subagent
    cron_job_name: NotRequired[str]


class ConfigMiddleware(AgentMiddleware[ConfigState, Any]):
    """Load user config and manage heartbeat lifecycle on session start.

    before_agent:
      - Reads config from volume, reconciles heartbeat cron schedule.
      - On main sessions: auto-creates heartbeat cron if missing.
      - On heartbeat sessions: checks active hours + HEARTBEAT.md content,
        early-exits if outside hours or empty.

    Mid-run config changes are handled at their source:
      - Agent tool path: manage_config applies side-effects and updates
        state via Command (fully self-contained).
      - Dashboard path: Next.js API route applies side-effects server-side
        after writing the config file.

    Must run AFTER ModalSandboxMiddleware (needs sandbox_id).
    Must run AFTER SessionSetupMiddleware (needs prompt_files for HEARTBEAT.md).
    """

    state_schema = ConfigState

    # -- Heartbeat helpers ---------------------------------------------------

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

    def _ensure_heartbeat_cron(self, config: UserConfig) -> None:
        """Auto-create heartbeat cron if none exists (first-run setup)."""
        if config.heartbeat.every == "off":
            return
        try:
            from agent.tools import _get_supabase

            sb = _get_supabase()
            result = sb.rpc("list_agent_crons").execute()
            jobs = result.data or []

            for job in jobs:
                if "heartbeat" in (job.get("jobname") or "").lower():
                    return  # Already exists

            schedule_expr = _parse_every_to_cron(config.heartbeat.every)

            sb.rpc("create_cron_session_job", {
                "job_name": "heartbeat",
                "schedule_expr": schedule_expr,
                "input_message": HEARTBEAT_INPUT_MESSAGE,
                "session_type": "cron",
            }).execute()
            logger.info("[Config] auto-created heartbeat cron (%s)", schedule_expr)

        except Exception as e:
            logger.warning("[Config] failed to ensure heartbeat cron: %s", e)

    # -- before_agent --------------------------------------------------------

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self, state: ConfigState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Load config, manage heartbeat, reconcile cron on session start."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            logger.warning("[Config] no sandbox_id, skipping config load")
            return None

        config = load_config(sandbox_id)
        reconcile_heartbeat_cron(config)

        session_type = state.get("session_type")

        # On main sessions: ensure heartbeat cron exists (auto-create if missing)
        if session_type in (None, "main"):
            self._ensure_heartbeat_cron(config)
            return {"config": config.model_dump(exclude_none=True)}

        # On heartbeat cron sessions: check active hours + content
        if state.get("cron_job_name") == "heartbeat":
            if not is_within_active_hours(config):
                logger.info("[Config] heartbeat outside active hours, early exit")
                return {
                    "config": config.model_dump(exclude_none=True),
                    "jump_to": "end",
                }

            if self._is_heartbeat_empty(state):
                logger.info("[Config] HEARTBEAT.md empty, early exit")
                return {
                    "config": config.model_dump(exclude_none=True),
                    "jump_to": "end",
                }

        # All other sessions: just store config
        return {"config": config.model_dump(exclude_none=True)}

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: ConfigState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync."""
        return self.before_agent(state, runtime)

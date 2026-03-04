"""Heartbeat middleware — detect heartbeat sessions, early exit if empty, auto-create cron."""

from __future__ import annotations

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

HEARTBEAT_INPUT_MESSAGE = (
    "Read HEARTBEAT.md from your project context and execute any tasks listed. "
    "Do not infer or repeat old tasks from prior sessions."
)


class HeartbeatState(AgentState):
    """State schema for heartbeat middleware."""

    session_type: NotRequired[str]  # main | task | cron | heartbeat | subagent


class HeartbeatMiddleware(AgentMiddleware[HeartbeatState, Any]):
    """Middleware that handles heartbeat sessions.

    - On heartbeat sessions (session_type="heartbeat"): checks if HEARTBEAT.md is
      empty and early-exits if so (zero cost). Otherwise lets the agent run — the
      Edge Function already injected delivery instructions into the message.
    - On first main session: auto-creates the heartbeat cron job if none exists.

    Must run AFTER SessionSetupMiddleware so that prompt_files are already loaded.
    """

    state_schema = HeartbeatState

    def _is_heartbeat_empty(self, state: dict) -> bool:
        """Check if HEARTBEAT.md is empty or missing using already-loaded prompt_files."""
        prompt_files = state.get("prompt_files") or {}
        content = prompt_files.get("HEARTBEAT.md", "").strip()
        if not content:
            return True
        # Strip comments and headings — if only comments/headings remain, treat as empty
        lines = [
            line
            for line in content.split("\n")
            if line.strip()
            and not line.strip().startswith("#")
            and not line.strip().startswith("<!--")
        ]
        return len(lines) == 0

    def _ensure_heartbeat_cron(self) -> None:
        """Auto-create heartbeat cron if none exists (first-run setup)."""
        try:
            from agent.tools import _get_supabase

            sb = _get_supabase()
            result = sb.rpc("list_agent_crons").execute()
            jobs = result.data or []

            for job in jobs:
                if "heartbeat" in (job.get("jobname") or "").lower():
                    return  # Already exists

            sb.rpc("create_cron_session_job", {
                "job_name": "heartbeat",
                "schedule_expr": "*/30 * * * *",
                "input_message": HEARTBEAT_INPUT_MESSAGE,
                "session_type": "heartbeat",
            }).execute()
            logger.info("[Heartbeat] auto-created heartbeat cron (*/30 * * * *)")

        except Exception as e:
            logger.warning("[Heartbeat] failed to ensure heartbeat cron: %s", e)

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self, state: HeartbeatState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Handle heartbeat sessions and auto-create cron on main sessions."""
        session_type = state.get("session_type")

        # On main sessions: ensure heartbeat cron exists (auto-create if missing)
        if session_type in (None, "main"):
            self._ensure_heartbeat_cron()
            return None

        if session_type != "heartbeat":
            return None  # Not a heartbeat session, pass through

        # Heartbeat session: early-exit if HEARTBEAT.md is empty (zero cost)
        if self._is_heartbeat_empty(state):
            logger.info("[Heartbeat] HEARTBEAT.md empty, early exit")
            return {"jump_to": "end"}

        # HEARTBEAT.md has content — let the agent run with the injected message
        return None

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: HeartbeatState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync."""
        return self.before_agent(state, runtime)

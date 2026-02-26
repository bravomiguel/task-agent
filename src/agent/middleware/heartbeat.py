"""Heartbeat middleware — detect heartbeat runs, early exit if empty, auto-create cron."""

from __future__ import annotations

import logging
import os
from typing import Any, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

HEARTBEAT_MARKER = "[HEARTBEAT]"


class HeartbeatState(AgentState):
    """State schema for heartbeat middleware."""

    session_type: NotRequired[str]  # main | task | cron | heartbeat


class HeartbeatMiddleware(AgentMiddleware[HeartbeatState, Any]):
    """Middleware that detects heartbeat runs and short-circuits if HEARTBEAT.md is empty.

    Must run AFTER ModalSandboxMiddleware (needs sandbox) and BEFORE SessionSetupMiddleware
    (sets session_type before archiving logic).
    """

    state_schema = HeartbeatState

    def _get_last_human_content(self, messages: list) -> str | None:
        """Extract the content of the last human message."""
        for msg in reversed(messages):
            msg_type = getattr(msg, "type", None) or (
                msg.get("type") if isinstance(msg, dict) else None
            )
            msg_role = getattr(msg, "role", None) or (
                msg.get("role") if isinstance(msg, dict) else None
            )

            if msg_type == "human" or msg_role == "user":
                content = getattr(msg, "content", None) or (
                    msg.get("content", "") if isinstance(msg, dict) else ""
                )
                return content if isinstance(content, str) else ""
        return None

    def _is_heartbeat_empty(self, sandbox_id: str) -> bool:
        """Quick check: is HEARTBEAT.md empty or missing?"""
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            process = sandbox.exec(
                "cat", "/default-user/prompts/HEARTBEAT.md", timeout=10
            )
            process.wait()
            if process.returncode != 0:
                return True
            content = process.stdout.read().strip()
            # Strip comments and headings — if only comments/headings remain, treat as empty
            lines = [
                line
                for line in content.split("\n")
                if line.strip()
                and not line.strip().startswith("#")
                and not line.strip().startswith("<!--")
            ]
            return len(lines) == 0
        except Exception as e:
            logger.warning("[Heartbeat] failed to read HEARTBEAT.md: %s", e)
            return True

    def _ensure_heartbeat_cron(self, session_id: str) -> None:
        """Auto-create heartbeat cron if none exists (first-run setup)."""
        try:
            from agent.cron_tools import _get_supabase

            sb = _get_supabase()
            result = sb.rpc("list_agent_crons").execute()
            jobs = result.data or []

            for job in jobs:
                if "heartbeat" in (job.get("jobname") or "").lower():
                    return  # Already exists

            sb.rpc("create_agent_cron", {
                "job_name": "heartbeat",
                "schedule_expr": "*/30 * * * *",
                "thread_id": session_id,
                "user_message": HEARTBEAT_MARKER,
            }).execute()
            logger.info("[Heartbeat] auto-created heartbeat cron (*/30 * * * *)")

        except Exception as e:
            logger.warning("[Heartbeat] failed to ensure heartbeat cron: %s", e)

    @hook_config(can_jump_to=["end"])
    def before_agent(
        self, state: HeartbeatState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Detect heartbeat runs and early-exit if HEARTBEAT.md is empty."""
        messages = state.get("messages", [])
        content = self._get_last_human_content(messages)

        if not content or HEARTBEAT_MARKER not in content:
            return None  # Not a heartbeat run, pass through

        updates: dict[str, Any] = {"session_type": "heartbeat"}

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            logger.warning("[Heartbeat] no sandbox available, skipping")
            return {"jump_to": "end", **updates}

        # Quick empty check — if HEARTBEAT.md is empty, exit early (zero cost)
        if self._is_heartbeat_empty(sandbox_id):
            logger.info("[Heartbeat] HEARTBEAT.md empty, early exit")
            return {"jump_to": "end", **updates}

        # Auto-create heartbeat cron if none exists
        session_id = state.get("session_id")
        if session_id:
            self._ensure_heartbeat_cron(session_id)

        return updates

    @hook_config(can_jump_to=["end"])
    async def abefore_agent(
        self, state: HeartbeatState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync."""
        return self.before_agent(state, runtime)

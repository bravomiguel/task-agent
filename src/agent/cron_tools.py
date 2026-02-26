"""Cron management tool — mirrors OpenClaw's cron tool with 8 actions."""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated, Literal

from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger(__name__)

_supabase_client = None


def _get_supabase():
    """Lazy-init Supabase client from env vars."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _supabase_client = create_client(url, key)
    return _supabase_client


@tool
def manage_crons(
    action: Literal["status", "list", "add", "update", "remove", "run", "runs", "wake"],
    job_name: str = None,
    job_id: int = None,
    schedule: str = None,
    thread_id: str = None,
    input_message: str = None,
    active: bool = None,
    include_disabled: bool = False,
    text: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Manage cron jobs and wake events (use for reminders; when scheduling a reminder, write the input_message as something that will read like a reminder when it fires, and mention that it is a reminder depending on the time gap between setting and firing; include recent context in reminder text if appropriate).

    Args:
        action: One of: status, list, add, update, remove, run, runs, wake.
        job_name: Name for the cron job (required for add/remove).
        job_id: Job ID (required for update/run/runs).
        schedule: Cron expression (required for add, optional for update).
        thread_id: Thread ID for the agent run. Defaults to current session.
        input_message: Message sent to agent when cron fires (required for add).
        active: Enable/disable a job (for update).
        include_disabled: Include disabled jobs in list (default: False).
        text: Message text for wake action (required for wake).

    Returns:
        JSON result or error message.
    """
    try:
        sb = _get_supabase()
    except Exception as e:
        return f"Error: Supabase not configured: {e}"

    default_thread_id = state.get("session_id") if state else None

    try:
        if action == "status":
            result = sb.rpc("get_cron_status").execute()
            data = result.data
            if data and len(data) > 0:
                row = data[0]
                return json.dumps({
                    "total_jobs": row["total_jobs"],
                    "active_jobs": row["active_jobs"],
                    "inactive_jobs": row["inactive_jobs"],
                })
            return json.dumps({"total_jobs": 0, "active_jobs": 0, "inactive_jobs": 0})

        elif action == "list":
            result = sb.rpc("list_agent_crons").execute()
            jobs = result.data or []
            if not include_disabled:
                jobs = [j for j in jobs if j.get("active", True)]
            return json.dumps(jobs, default=str)

        elif action == "add":
            if not job_name:
                return "Error: job_name is required for add."
            if not schedule:
                return "Error: schedule is required for add."
            if not input_message:
                return "Error: input_message is required for add."
            tid = thread_id or default_thread_id
            if not tid:
                return "Error: thread_id is required (no default session available)."
            result = sb.rpc("create_agent_cron", {
                "job_name": job_name,
                "schedule_expr": schedule,
                "thread_id": tid,
                "user_message": input_message,
            }).execute()
            return json.dumps({"job_id": result.data, "job_name": job_name, "schedule": schedule})

        elif action == "update":
            if job_id is None:
                return "Error: job_id is required for update."
            params = {"job_id": job_id}
            if schedule is not None:
                params["new_schedule"] = schedule
            if active is not None:
                params["new_active"] = active
            sb.rpc("update_agent_cron", params).execute()
            return json.dumps({"updated": True, "job_id": job_id})

        elif action == "remove":
            if not job_name:
                return "Error: job_name is required for remove."
            result = sb.rpc("delete_agent_cron", {"job_name": job_name}).execute()
            return json.dumps({"removed": True, "job_name": job_name})

        elif action == "run":
            if job_id is None:
                return "Error: job_id is required for run."
            sb.rpc("run_agent_cron", {"job_id": job_id}).execute()
            return json.dumps({"triggered": True, "job_id": job_id})

        elif action == "runs":
            if job_id is None:
                return "Error: job_id is required for runs."
            result = sb.rpc("get_agent_cron_runs", {"p_job_id": job_id}).execute()
            return json.dumps(result.data or [], default=str)

        elif action == "wake":
            if not text:
                return "Error: text is required for wake."
            tid = thread_id or default_thread_id
            if not tid:
                return "Error: thread_id is required (no default session available)."
            result = sb.rpc("wake_agent", {
                "thread_id": tid,
                "wake_text": text,
            }).execute()
            return json.dumps({"request_id": result.data, "thread_id": tid})

        else:
            return f"Error: Unknown action '{action}'."

    except Exception as e:
        logger.warning("[manage_crons] %s failed: %s", action, e)
        return f"Error: {action} failed: {e}"

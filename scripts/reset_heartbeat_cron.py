#!/usr/bin/env python3
"""Ensure heartbeat cron exists in Supabase with default 30m schedule.

Deletes any existing heartbeat cron and recreates it fresh.
Requires SUPABASE_URL and SUPABASE_SERVICE_KEY env vars.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_SERVICE_KEY")
if not url or not key:
    print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set", file=sys.stderr)
    sys.exit(1)

from supabase import create_client

sb = create_client(url, key)

HEARTBEAT_INPUT_MESSAGE = (
    "Read HEARTBEAT.md from your project context and execute any tasks listed. "
    "Do not infer or repeat old tasks from prior sessions."
)

# Remove existing heartbeat cron if any
try:
    result = sb.rpc("list_agent_crons").execute()
    for job in result.data or []:
        if "heartbeat" in (job.get("jobname") or "").lower():
            sb.rpc("delete_agent_cron", {"job_name": job["jobname"]}).execute()
            print(f"Deleted existing heartbeat cron (job_id={job.get('jobid')})")
except Exception as e:
    print(f"Warning: could not check existing crons: {e}", file=sys.stderr)

# Default active hours config (matches config.py defaults)
DEFAULT_TIMEZONE = "UTC"
DEFAULT_ACTIVE_HOURS_START = "09:00"
DEFAULT_ACTIVE_HOURS_END = "21:00"

# Create fresh heartbeat cron at default 30m with active hours
try:
    result = sb.rpc("create_cron_session_job", {
        "job_name": "heartbeat",
        "schedule_expr": "*/30 * * * *",
        "input_message": HEARTBEAT_INPUT_MESSAGE,
        "session_type": "cron",
        "timezone": DEFAULT_TIMEZONE,
        "active_hours_start": DEFAULT_ACTIVE_HOURS_START,
        "active_hours_end": DEFAULT_ACTIVE_HOURS_END,
    }).execute()
    print(f"Created heartbeat cron (*/30 * * * *, job_id={result.data})")
except Exception as e:
    print(f"Error: failed to create heartbeat cron: {e}", file=sys.stderr)
    sys.exit(1)

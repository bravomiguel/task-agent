"""Agent tools."""

from __future__ import annotations

import json as _json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

import httpx
import modal
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger(__name__)

LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://localhost:2024")


def _get_mime_type(filepath: str) -> str:
    """Get MIME type from file extension."""
    mime_type, _ = mimetypes.guess_type(filepath)
    return mime_type or "application/octet-stream"


@tool
def present_file(filepath: str) -> str:
    """Present a file to the user in the document viewer.

    Call this tool after creating or modifying a file that the user should see.
    The file will automatically open in the user's document viewer.

    Args:
        filepath: Relative path to the file (e.g., "outputs/report.md").
                  Must be a file in the outputs/ directory.

    Returns:
        XML with file metadata for frontend rendering.
    """
    # Extract filename from path
    name = os.path.basename(filepath)
    mime_type = _get_mime_type(filepath)

    return f"""<presented_file>
<file_path>{filepath}</file_path>
<name>{name}</name>
<mime_type>{mime_type}</mime_type>
</presented_file>"""


def _extract_event_content(state: dict) -> str | None:
    """Extract event content from the first user message."""
    messages = state.get("messages", [])
    for msg in messages:
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
            return content
    return None


@tool
def route_event(
    thread_id: str,
    task_instruction: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Route the incoming event to a thread and start the task agent.

    Call this tool when you have decided which thread to route the event to.
    The tool will execute the routing and return success or an error message.
    If you get an error, you may retry up to 2 more times.

    Args:
        thread_id: Use 'new' for a new thread, or provide an existing thread UUID.
        task_instruction: Optional brief instruction for the task agent when you want
            it to focus on a specific part of the event. Omit when the task agent
            should process the entire event.

    Returns:
        Success message with details, or error message if something went wrong.
    """
    api_url = LANGGRAPH_API_URL

    if not thread_id:
        return "Error: thread_id is required. Use 'new' or an existing thread UUID."

    # Extract event content from messages (raw XML)
    event_content = _extract_event_content(state) if state else None
    if not event_content:
        return "Error: Could not extract event content from messages."

    # Build user message: instruction (if provided) + event XML
    if task_instruction:
        user_message = f"{task_instruction}\n\n{event_content}"
    else:
        user_message = event_content

    # Execute routing
    try:
        target_thread_id = thread_id

        if thread_id == "new":
            # Create new thread
            response = httpx.post(
                f"{api_url}/threads",
                headers={"Content-Type": "application/json"},
                json={},
                timeout=30,
            )
            response.raise_for_status()
            target_thread_id = response.json()["thread_id"]

        # Create run on thread with user message
        response = httpx.post(
            f"{api_url}/threads/{target_thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"role": "user", "content": user_message}]},
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()

        if thread_id == "new":
            return f"Success: Created new thread {target_thread_id} and started task agent."
        else:
            return f"Success: Routed to existing thread {target_thread_id} and started task agent."

    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out. The API at {api_url} may be slow or unavailable."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: Unexpected error during routing: {e}"


NEXTJS_API_URL = os.getenv("NEXTJS_API_URL", "http://localhost:3000")


@tool
def view_image(
    filepath: str,
    detail: Literal["high", "low", "auto"] = "high",
    state: Annotated[dict, InjectedState] = None,
) -> list[dict]:
    """View and analyze an image file.

    Call this tool when you need to visually examine an image to understand its
    contents, extract information, or answer questions about it. The image will
    be processed and returned for your visual analysis.

    Args:
        filepath: Path to the image file (e.g., "uploads/screenshot.png").
        detail: Level of detail for analysis. Use "high" for detailed analysis
                of complex images, "low" for simple/quick viewing, "auto" to
                let the system decide.

    Returns:
        Image content block that you can analyze visually.
    """
    if state is None:
        return [{"type": "text", "text": "Error: Could not access state."}]

    session_id = state.get("session_id")
    if session_id is None:
        return [{"type": "text", "text": "Error: Session ID not available."}]

    # Normalize filepath to relative path (strip /default-user/session-storage/{id}/ prefix if present)
    normalized_path = filepath
    if filepath.startswith("/default-user/session-storage/"):
        parts = filepath.split("/", 5)  # ['', 'default-user', 'session-storage', 'id', 'uploads', 'file.png']
        if len(parts) >= 6:
            normalized_path = "/".join(parts[4:])  # 'uploads/file.png'
    elif filepath.startswith("default-user/session-storage/"):
        parts = filepath.split("/", 4)  # ['default-user', 'session-storage', 'id', 'uploads', 'file.png']
        if len(parts) >= 5:
            normalized_path = "/".join(parts[3:])  # 'uploads/file.png'

    try:
        # Call the Next.js API to get image base64
        response = httpx.get(
            f"{NEXTJS_API_URL}/api/images/base64",
            params={
                "thread_id": session_id,
                "path": normalized_path,
                "detail": detail,
            },
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        b64_data = data.get("base64")
        mime_type = data.get("mime", "image/png")

        if not b64_data:
            return [{"type": "text", "text": "Error: Failed to get image"}]

        # Return in OpenAI vision format
        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64_data}",
                    "detail": detail,
                },
            }
        ]

    except httpx.HTTPStatusError as e:
        error_detail = e.response.text
        return [{"type": "text", "text": f"Error processing image: {error_detail}"}]
    except Exception as e:
        return [{"type": "text", "text": f"Error viewing image: {e}"}]


# ---------------------------------------------------------------------------
# Memory search
# ---------------------------------------------------------------------------

_MEMORY_SCRIPT_PATH = Path(__file__).parent / "memory" / "_sandbox_script.py"
_memory_script_cache: str | None = None


def _load_memory_script() -> str:
    global _memory_script_cache
    if _memory_script_cache is None:
        _memory_script_cache = _MEMORY_SCRIPT_PATH.read_text()
    return _memory_script_cache


@tool
def memory_search(
    query: str,
    max_results: int = 6,
    min_score: float = 0.35,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Mandatory recall step: semantically search MEMORY.md + memory/*.md before answering questions about prior work, decisions, dates, people, preferences, or todos; returns top snippets with path + lines.

    Args:
        query: Natural language description of what you're looking for.
        max_results: Maximum number of results to return (default: 6).
        min_score: Minimum relevance score threshold 0-1 (default: 0.35).

    Returns:
        JSON with results array containing path, startLine, endLine, score, snippet, source.
        Use read_file to get full context for any relevant result.
    """
    if state is None:
        return "Error: Could not access state."

    sandbox_id = state.get("modal_sandbox_id")
    if not sandbox_id:
        return "Error: No sandbox available."

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return "Error: No OpenAI API key available for embedding."

    script = _load_memory_script()

    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        sandbox.reload_volumes()

        process = sandbox.exec(
            "python3", "-", "search",
            "--query", query,
            "--max-results", str(max_results),
            "--min-score", str(min_score),
            "--api-key", api_key,
            timeout=30,
        )
        process.stdin.write(script.encode())
        process.stdin.write_eof()
        process.stdin.drain()
        process.wait()

        stdout = process.stdout.read()
        stderr = process.stderr.read()

        if stderr:
            logger.warning("[MemorySearch] stderr: %s", stderr[:500])

        if process.returncode != 0:
            logger.warning("[MemorySearch] failed (rc=%d): %s", process.returncode, stderr[:500])
            return f"Error searching memory: {stderr[:200]}"

        return stdout

    except Exception as exc:
        logger.warning("[MemorySearch] error: %s", exc)
        return f"Error searching memory: {exc}"


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


@tool
def sessions_list(limit: int = 20, offset: int = 0) -> str:
    """List recent session threads. Use limit/offset to page through results.

    Each result includes session_id, session_type, status (idle/busy/error),
    and updated_at. Inspect session_type to find threads of a specific kind
    (e.g. "main", "cron", "heartbeat"). Sort by updated_at to find the latest.

    Args:
        limit: Number of threads to return (default 20).
        offset: Pagination offset (default 0).

    Returns:
        JSON array of sessions, each with session_id, session_type, status, updated_at.
    """
    api_url = LANGGRAPH_API_URL
    try:
        response = httpx.post(
            f"{api_url}/threads/search",
            headers={"Content-Type": "application/json"},
            json={"limit": limit, "offset": offset},
            timeout=30,
        )
        response.raise_for_status()
        threads = response.json()
        trimmed = []
        for t in threads:
            values = t.get("values") or {}
            trimmed.append({
                "session_id": t.get("thread_id"),
                "session_type": values.get("session_type"),
                "status": t.get("status"),
                "updated_at": t.get("updated_at"),
            })
        return _json.dumps(trimmed)
    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out connecting to {api_url}."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


@tool
def sessions_send(thread_id: str, message: str) -> str:
    """Send a message into an existing session thread (fire-and-forget).

    Use sessions_list first to find the target thread_id. Returns immediately
    after submitting — does not wait for the agent to respond. On error (e.g.
    thread busy or not found), returns an error string so you can fall back to
    sessions_spawn.

    Args:
        thread_id: The session ID to send to (from sessions_list).
        message: The message content to deliver.

    Returns:
        JSON with status, thread_id, and run_id on success; error string on failure.
    """
    api_url = LANGGRAPH_API_URL
    try:
        response = httpx.post(
            f"{api_url}/threads/{thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"role": "user", "content": message}]},
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        return _json.dumps({
            "status": "accepted",
            "thread_id": thread_id,
            "run_id": data.get("run_id"),
        })
    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out connecting to {api_url}."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


@tool
def sessions_spawn(
    message: str,
    session_type: Literal["main", "task", "cron", "heartbeat", "subagent"] = "main",
) -> str:
    """Create a new session thread and start a run (fire-and-forget).

    Use this to start a fresh session, or as a fallback when sessions_send
    fails (e.g. the target thread is busy). Creates the thread then immediately
    starts a run with the given message and session_type.

    Args:
        message: The message to send to the new session.
        session_type: Type of session to create (default "main").

    Returns:
        JSON with status, thread_id, and run_id on success; error string on failure.
    """
    api_url = LANGGRAPH_API_URL
    try:
        # Step 1: create thread
        r1 = httpx.post(
            f"{api_url}/threads",
            headers={"Content-Type": "application/json"},
            json={},
            timeout=30,
        )
        r1.raise_for_status()
        thread_id = r1.json()["thread_id"]

        # Step 2: start run with session_type in input
        r2 = httpx.post(
            f"{api_url}/threads/{thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "agent",
                "input": {
                    "messages": [{"role": "user", "content": message}],
                    "session_type": session_type,
                },
                "stream_resumable": True,
            },
            timeout=30,
        )
        r2.raise_for_status()
        data = r2.json()
        return _json.dumps({
            "status": "accepted",
            "thread_id": thread_id,
            "run_id": data.get("run_id"),
        })
    except httpx.ConnectError as e:
        return f"Error: Could not connect to LangGraph API at {api_url}. Details: {e}"
    except httpx.TimeoutException:
        return f"Error: Request timed out connecting to {api_url}."
    except httpx.HTTPStatusError as e:
        return f"Error: API returned status {e.response.status_code}. Details: {e.response.text}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Cron tools
# ---------------------------------------------------------------------------

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


def _parse_at_schedule(dt_str: str) -> str:
    """Convert UTC datetime string to a cron expression for that exact minute.

    E.g. "2026-03-03T17:30:00Z" → "30 17 3 3 *"
    """
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    if dt_utc <= datetime.now(timezone.utc):
        raise ValueError("Cannot schedule in the past.")
    return f"{dt_utc.minute} {dt_utc.hour} {dt_utc.day} {dt_utc.month} *"


def _parse_every_schedule(interval_str: str) -> str:
    """Convert interval string (e.g. '5m', '2h', '1d') to cron expression."""
    m = re.fullmatch(r"(\d+)\s*([smhd])", interval_str.strip().lower())
    if not m:
        raise ValueError(
            f"Invalid interval format: {interval_str!r}. Use e.g. '5m', '2h', '1d'."
        )
    value, unit = int(m.group(1)), m.group(2)
    if value <= 0:
        raise ValueError("Interval must be positive.")
    if unit == "s":
        mins = max(1, value // 60)
        return f"*/{mins} * * * *" if mins < 60 else f"0 */{mins // 60} * * *"
    elif unit == "m":
        if value < 60:
            return f"*/{value} * * * *"
        return f"0 */{value // 60} * * *"
    elif unit == "h":
        if value < 24:
            return f"0 */{value} * * *"
        return f"0 0 */{value // 24} * *"
    elif unit == "d":
        return f"0 0 */{value} * *"
    raise ValueError(f"Unsupported unit: {unit}")


@tool
def manage_crons(
    action: Literal["status", "list", "add", "update", "remove", "run", "runs", "wake"],
    job_name: str = None,
    job_id: int = None,
    schedule: str = None,
    schedule_type: Literal["cron", "at", "every"] = "cron",
    thread_id: str = None,
    input_message: str = None,
    active: bool = None,
    include_disabled: bool = False,
    text: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Manage cron jobs and wake events (use for reminders; when scheduling a reminder, write the input_message as something that will read like a reminder when it fires, and mention that it is a reminder depending on the time gap between setting and firing; include recent context in reminder text if appropriate).

    add: Creates an isolated cron session — a fresh thread fires each time the
    schedule triggers. The cron agent receives the input_message with [CRON:job_name]
    tagging and instructions to deliver a summary back to the main session when done.

    Args:
        action: One of: status, list, add, update, remove, run, runs, wake.
        job_name: Name for the cron job (required for add/remove).
        job_id: Job ID (required for update/run/runs).
        schedule: Schedule expression (required for add, optional for update).
            - schedule_type="cron": standard cron expression (e.g., "*/5 * * * *")
            - schedule_type="at": UTC datetime ISO-8601 (e.g., "2026-03-03T17:30:00Z") — fires once then auto-deletes
            - schedule_type="every": interval string (e.g., "5m", "2h", "1d")
        schedule_type: How to interpret 'schedule': "cron" (default), "at" (one-shot), or "every" (interval).
        thread_id: Thread ID for wake action (defaults to current session).
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
                return _json.dumps({
                    "total_jobs": row["total_jobs"],
                    "active_jobs": row["active_jobs"],
                    "inactive_jobs": row["inactive_jobs"],
                })
            return _json.dumps({"total_jobs": 0, "active_jobs": 0, "inactive_jobs": 0})

        elif action == "list":
            result = sb.rpc("list_agent_crons").execute()
            jobs = result.data or []
            if not include_disabled:
                jobs = [j for j in jobs if j.get("active", True)]
            return _json.dumps(jobs, default=str)

        elif action == "add":
            if not job_name:
                return "Error: job_name is required for add."
            if not schedule:
                return "Error: schedule is required for add."
            if not input_message:
                return "Error: input_message is required for add."

            once = False
            if schedule_type == "at":
                try:
                    cron_expr = _parse_at_schedule(schedule)
                except Exception as e:
                    return f"Error: Invalid datetime for 'at': {e}"
                once = True
            elif schedule_type == "every":
                try:
                    cron_expr = _parse_every_schedule(schedule)
                except Exception as e:
                    return f"Error: Invalid interval for 'every': {e}"
            else:
                cron_expr = schedule

            result = sb.rpc("create_cron_session_job", {
                "job_name": job_name,
                "schedule_expr": cron_expr,
                "input_message": input_message,
                "once": once,
            }).execute()
            response = {"job_id": result.data, "job_name": job_name, "schedule": cron_expr}
            if schedule_type != "cron":
                response["original_schedule"] = schedule
                response["schedule_type"] = schedule_type
            return _json.dumps(response)

        elif action == "update":
            if job_id is None:
                return "Error: job_id is required for update."
            params = {"job_id": job_id}
            if schedule is not None:
                params["new_schedule"] = schedule
            if active is not None:
                params["new_active"] = active
            sb.rpc("update_agent_cron", params).execute()
            return _json.dumps({"updated": True, "job_id": job_id})

        elif action == "remove":
            if not job_name:
                return "Error: job_name is required for remove."
            sb.rpc("delete_agent_cron", {"job_name": job_name}).execute()
            return _json.dumps({"removed": True, "job_name": job_name})

        elif action == "run":
            if job_id is None:
                return "Error: job_id is required for run."
            sb.rpc("run_agent_cron", {"job_id": job_id}).execute()
            return _json.dumps({"triggered": True, "job_id": job_id})

        elif action == "runs":
            if job_id is None:
                return "Error: job_id is required for runs."
            result = sb.rpc("get_agent_cron_runs", {"p_job_id": job_id}).execute()
            return _json.dumps(result.data or [], default=str)

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
            return _json.dumps({"request_id": result.data, "thread_id": tid})

        else:
            return f"Error: Unknown action '{action}'."

    except Exception as e:
        logger.warning("[manage_crons] %s failed: %s", action, e)
        return f"Error: {action} failed: {e}"

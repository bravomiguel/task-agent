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

_SYSTEM_MESSAGE_RE = re.compile(r"\s*<system-message[^>]*>.*?</system-message>", re.DOTALL)
_TEXT_MAX_CHARS = 4000


def _sanitize_content(content) -> str | None:
    """Sanitize message content: extract text, strip system-message tags, truncate, drop image data."""
    if isinstance(content, str):
        text = _SYSTEM_MESSAGE_RE.sub("", content).strip()
        if len(text) > _TEXT_MAX_CHARS:
            text = text[:_TEXT_MAX_CHARS] + "… [truncated]"
        return text or None

    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    parts.append(block)
                continue
            btype = block.get("type", "")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype in ("image", "image_url"):
                parts.append("[image omitted]")
            # skip tool_use, tool_result, etc.
        text = " ".join(p for p in parts if p)
        text = _SYSTEM_MESSAGE_RE.sub("", text).strip()
        if len(text) > _TEXT_MAX_CHARS:
            text = text[:_TEXT_MAX_CHARS] + "… [truncated]"
        return text or None

    return None


def _extract_messages(
    messages: list[dict], limit: int, include_tools: bool = False,
) -> list[dict]:
    """Extract messages, filtering tool messages and sanitizing content."""
    filtered = []
    for m in messages:
        role = m.get("type") or m.get("role", "")
        if not include_tools and role not in ("human", "ai", "user", "assistant"):
            continue

        content = _sanitize_content(m.get("content", ""))
        if not content:
            continue

        if role in ("human", "user"):
            label = "user"
        elif role in ("ai", "assistant"):
            label = "assistant"
        else:
            label = role

        entry: dict = {"role": label, "content": content}
        ts = m.get("created_at") or m.get("timestamp")
        if ts:
            entry["timestamp"] = str(ts)
        filtered.append(entry)

    return filtered[-limit:]


@tool
def sessions_list(
    limit: int = 20,
    offset: int = 0,
    session_type: str = None,
    message_limit: int = 0,
) -> str:
    """List recent session threads with optional filters.

    Args:
        limit: Number of threads to return (default 20).
        offset: Pagination offset (default 0).
        session_type: Filter by session type (e.g. "main", "cron", "heartbeat").
            Omit to return all types.
        message_limit: Include last N messages per session (default 0, max 20).
            Useful to peek at recent conversation without a separate call.

    Returns:
        JSON array of sessions, each with session_id, session_type, status,
        updated_at, and optionally last_messages.
    """
    api_url = LANGGRAPH_API_URL
    try:
        payload: dict = {
            "limit": limit,
            "offset": offset,
            "sort_by": "updated_at",
            "sort_order": "desc",
        }
        if session_type:
            payload["values"] = {"session_type": session_type}

        response = httpx.post(
            f"{api_url}/threads/search",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        threads = response.json()
        trimmed = []
        for t in threads:
            values = t.get("values") or {}
            entry = {
                "session_id": t.get("thread_id"),
                "session_type": values.get("session_type"),
                "status": t.get("status"),
                "updated_at": t.get("updated_at"),
            }
            if message_limit > 0:
                messages = values.get("messages", [])
                entry["last_messages"] = _extract_messages(messages, min(message_limit, 20))
            trimmed.append(entry)
        return _json.dumps(trimmed)
    except httpx.ConnectError as e:
        return _json.dumps({"status": "error", "error": f"Connection failed: {e}"})
    except httpx.TimeoutException:
        return _json.dumps({"status": "error", "error": "Request timed out"})
    except httpx.HTTPStatusError as e:
        return _json.dumps({"status": "error", "error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)})


def _wrap_origin_message(tool_name: str, message: str, state: dict | None) -> str:
    """Wrap message in a <system-message> tag with origin context from agent state.

    The type attribute is the tool name (e.g. "sessions-send", "sessions-spawn").
    Always includes session_id and session_type. Includes cron_job_id,
    cron_job_name, and schedule_type only when present (cron/heartbeat sessions).
    """
    if not state:
        return message
    attrs = [
        f'type="{tool_name}"',
        f'session_id="{state.get("session_id", "")}"',
        f'session_type="{state.get("session_type", "")}"',
    ]
    cron_job_id = state.get("cron_job_id")
    if cron_job_id is not None:
        attrs.append(f'cron_job_id="{cron_job_id}"')
    cron_job_name = state.get("cron_job_name")
    if cron_job_name is not None:
        attrs.append(f'cron_job_name="{cron_job_name}"')
    cron_schedule_type = state.get("cron_schedule_type")
    if cron_schedule_type is not None:
        attrs.append(f'schedule_type="{cron_schedule_type}"')
    attr_str = " ".join(attrs)
    return f"<system-message {attr_str}>\n{message}\n</system-message>"


def _extract_last_ai_message(thread_id: str, api_url: str) -> str | None:
    """Fetch the last AI message content from a thread's state."""
    try:
        r = httpx.get(
            f"{api_url}/threads/{thread_id}/state",
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        messages = r.json().get("values", {}).get("messages", [])
        for msg in reversed(messages):
            if msg.get("type") == "ai":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                    return " ".join(parts) if parts else None
                return None
    except Exception:
        pass
    return None


def _wait_for_run(thread_id: str, run_id: str, timeout_seconds: int, api_url: str) -> dict:
    """Wait for a run to complete and return the result with optional reply."""
    try:
        r = httpx.get(
            f"{api_url}/threads/{thread_id}/runs/{run_id}/join",
            timeout=timeout_seconds + 5,
        )
        r.raise_for_status()
        reply = _extract_last_ai_message(thread_id, api_url)
        result = {"status": "ok", "thread_id": thread_id, "run_id": run_id}
        if reply:
            result["reply"] = reply
        return result
    except httpx.TimeoutException:
        return {"status": "timeout", "thread_id": thread_id, "run_id": run_id}
    except Exception as e:
        return {"status": "error", "thread_id": thread_id, "run_id": run_id, "error": str(e)}


@tool
def sessions_send(
    thread_id: str,
    message: str,
    timeout_seconds: int = 0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Send a message to another session.

    Use sessions_list first to find the target thread_id. By default fires and
    forgets (timeout_seconds=0). Set timeout_seconds > 0 to wait for the
    agent's reply.

    Args:
        thread_id: The session ID to send to (from sessions_list).
        message: The message content to deliver.
        timeout_seconds: Seconds to wait for reply (0 = fire-and-forget).

    Returns:
        JSON with status (accepted/ok/timeout/error), thread_id, run_id,
        and optionally reply.
    """
    api_url = LANGGRAPH_API_URL
    full_message = _wrap_origin_message("sessions-send", message, state)
    try:
        response = httpx.post(
            f"{api_url}/threads/{thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "agent",
                "input": {"messages": [{"role": "user", "content": full_message}]},
                "stream_resumable": True,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        run_id = data.get("run_id")

        if timeout_seconds > 0 and run_id:
            return _json.dumps(_wait_for_run(thread_id, run_id, timeout_seconds, api_url))

        return _json.dumps({"status": "accepted", "thread_id": thread_id, "run_id": run_id})
    except httpx.ConnectError as e:
        return _json.dumps({"status": "error", "error": f"Connection failed: {e}"})
    except httpx.TimeoutException:
        return _json.dumps({"status": "error", "error": "Request timed out"})
    except httpx.HTTPStatusError as e:
        return _json.dumps({"status": "error", "error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)})


@tool
def sessions_spawn(
    message: str,
    session_type: Literal["main", "task", "cron", "heartbeat", "subagent"] = "main",
    timeout_seconds: int = 0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Spawn a new session.

    Creates a fresh thread and starts a run. Use as a fallback when
    sessions_send fails (e.g. thread busy). Set timeout_seconds > 0 to wait
    for the agent's reply.

    Args:
        message: The message to send to the new session.
        session_type: Type of session to create (default "main").
        timeout_seconds: Seconds to wait for reply (0 = fire-and-forget).

    Returns:
        JSON with status (accepted/ok/timeout/error), thread_id, run_id,
        and optionally reply.
    """
    api_url = LANGGRAPH_API_URL
    full_message = _wrap_origin_message("sessions-spawn", message, state)
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
                    "messages": [{"role": "user", "content": full_message}],
                    "session_type": session_type,
                },
                "stream_resumable": True,
            },
            timeout=30,
        )
        r2.raise_for_status()
        data = r2.json()
        run_id = data.get("run_id")

        if timeout_seconds > 0 and run_id:
            return _json.dumps(_wait_for_run(thread_id, run_id, timeout_seconds, api_url))

        return _json.dumps({"status": "accepted", "thread_id": thread_id, "run_id": run_id})
    except httpx.ConnectError as e:
        return _json.dumps({"status": "error", "error": f"Connection failed: {e}"})
    except httpx.TimeoutException:
        return _json.dumps({"status": "error", "error": "Request timed out"})
    except httpx.HTTPStatusError as e:
        return _json.dumps({"status": "error", "error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)})


@tool
def sessions_history(
    session_id: str,
    limit: int = 50,
    include_tools: bool = False,
) -> str:
    """Fetch message history for a session.

    Returns sanitized messages from the target session. Tool messages are
    excluded by default. Image data is stripped. Long text is truncated to
    4000 chars per message.

    Args:
        session_id: The session ID to fetch history for.
        limit: Max messages to return (default 50).
        include_tools: Include tool call/result messages (default False).

    Returns:
        JSON with session_id, messages array, and count.
    """
    api_url = LANGGRAPH_API_URL
    try:
        r = httpx.get(
            f"{api_url}/threads/{session_id}/state",
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        state = r.json()
        values = state.get("values") or {}
        raw_messages = values.get("messages", [])

        messages = _extract_messages(raw_messages, limit, include_tools=include_tools)

        return _json.dumps({
            "session_id": session_id,
            "session_type": values.get("session_type"),
            "messages": messages,
            "count": len(messages),
            "truncated": len(raw_messages) > limit,
        })
    except httpx.ConnectError as e:
        return _json.dumps({"status": "error", "error": f"Connection failed: {e}"})
    except httpx.TimeoutException:
        return _json.dumps({"status": "error", "error": "Request timed out"})
    except httpx.HTTPStatusError as e:
        return _json.dumps({"status": "error", "error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)})


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
    active_filter: Literal["all", "active", "inactive"] = "active",
    limit: int = 20,
    offset: int = 0,
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
            - schedule_type="at": UTC datetime ISO-8601 (e.g., "2026-03-03T17:30:00Z") — fires once then deactivated
            - schedule_type="every": interval string (e.g., "5m", "2h", "1d")
        schedule_type: How to interpret 'schedule': "cron" (default), "at" (one-shot), or "every" (interval).
        thread_id: Thread ID for wake action (defaults to current session).
        input_message: Message sent to agent when cron fires (required for add).
        active: Enable/disable a job (for update).
        active_filter: Filter list results: "active" (default), "inactive", or "all".
        limit: Max number of jobs to return for list action (default 20).
        offset: Pagination offset for list action (default 0).
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
            result = sb.rpc("list_agent_crons", {
                "p_limit": limit,
                "p_offset": offset,
            }).execute()
            jobs = result.data or []
            if active_filter == "active":
                jobs = [j for j in jobs if j.get("active", True)]
            elif active_filter == "inactive":
                jobs = [j for j in jobs if not j.get("active", True)]
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
                "schedule_type": schedule_type,
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

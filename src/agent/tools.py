"""Agent tools."""

from __future__ import annotations

import json as _json
import logging
import mimetypes
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

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

    # Normalize filepath to relative path (strip /mnt/session-storage/{id}/ prefix if present)
    normalized_path = filepath
    if filepath.startswith("/mnt/session-storage/"):
        parts = filepath.split("/", 5)  # ['', 'mnt', 'session-storage', 'id', 'uploads', 'file.png']
        if len(parts) >= 6:
            normalized_path = "/".join(parts[4:])  # 'uploads/file.png'
    elif filepath.startswith("mnt/session-storage/"):
        parts = filepath.split("/", 4)  # ['mnt', 'session-storage', 'id', 'uploads', 'file.png']
        if len(parts) >= 5:
            normalized_path = "/".join(parts[3:])  # 'uploads/file.png'

    try:
        # Call Modal encode_image function (replaces NextJS /api/images/base64)
        # Reads from volume, resizes with Pillow, returns base64 — via Modal RPC
        encode_fn = modal.Function.from_name("file-service", "encode_image")
        data = encode_fn.remote(session_id, normalized_path, detail)

        if data.get("error"):
            return [{"type": "text", "text": f"Error: {data['error']}"}]

        b64_data = data["base64"]
        mime_type = data.get("mime", "image/png")

        # Return LangChain standard ImageContentBlock format.
        # LangChain adapters (langchain_anthropic, langchain_openai) convert
        # this to the provider-specific format automatically.
        return [
            {
                "type": "image",
                "base64": b64_data,
                "mime_type": mime_type,
            }
        ]

    except Exception as e:
        return [{"type": "text", "text": f"Error viewing image: {e}"}]


# ---------------------------------------------------------------------------
# Memory search
# ---------------------------------------------------------------------------


@tool
def memory_search(
    query: str,
    max_results: int = 6,
    min_score: float = 0.35,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Mandatory recall step: semantically search memory files, session transcripts, and meeting transcripts before answering questions about prior work, decisions, dates, people, preferences, or todos; returns top snippets with path + lines.

    Args:
        query: Natural language description of what you're looking for.
        max_results: Maximum number of results to return (default: 6).
        min_score: Minimum relevance score threshold 0-1 (default: 0.35).

    Returns:
        JSON with results array containing path, startLine, endLine, score, snippet, source.
        Use read_file to get full context for any relevant result.
    """
    import json as _json
    from agent.memory.store import search_memory

    try:
        result = search_memory(
            query=query,
            max_results=max_results,
            min_score=min_score,
        )
        return _json.dumps(result)
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
        session_type: Filter by session type (e.g. "main", "subagent").
            Filters client-side. Omit to return all types.
        message_limit: Include last N messages per session (default 0, max 20).
            Useful to peek at recent conversation without a separate call.

    Returns:
        JSON array of sessions, each with session_id, session_type, status,
        updated_at, and optionally last_messages.
    """
    api_url = LANGGRAPH_API_URL
    try:
        # Fetch more threads when filtering client-side to ensure enough results
        fetch_limit = limit * 3 if session_type else limit
        payload: dict = {
            "limit": fetch_limit,
            "offset": offset,
            "sort_by": "updated_at",
            "sort_order": "desc",
        }

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
            thread_type = values.get("session_type")
            if session_type and thread_type != session_type:
                continue
            entry = {
                "session_id": t.get("thread_id"),
                "session_type": thread_type,
                "status": t.get("status"),
                "updated_at": t.get("updated_at"),
            }
            if message_limit > 0:
                messages = values.get("messages", [])
                entry["last_messages"] = _extract_messages(messages, min(message_limit, 20))
            trimmed.append(entry)
            if len(trimmed) >= limit:
                break
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
    cron_job_name, and schedule_type only when present (cron/heartbeat runs).
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


def _queue_for_thread(thread_id: str, message: str, state: dict | None, source: str = "sessions-send") -> dict:
    """Insert a message into inbound_queue for later delivery to a specific thread.

    Used as fallback when a thread is busy and can't accept a run immediately.
    """
    session_type = state.get("session_type", "main") if state else "main"
    cron_job_name = state.get("cron_job_name", "") if state else ""
    if "heartbeat" in cron_job_name.lower():
        priority = 5
    elif cron_job_name:
        priority = 4
    elif session_type == "subagent":
        priority = 3
    else:
        priority = 3

    try:
        sb = _get_supabase()
        sb.table("inbound_queue").insert({
            "source": source,
            "priority": priority,
            "thread_id": thread_id,
            "buffer_key": f"{source}:{thread_id}",
            "combined_text": message,
            "metadata": {
                "session_id": state.get("session_id") if state else None,
                "session_type": session_type,
            },
        }).execute()
        return {"status": "queued", "thread_id": thread_id, "priority": priority}
    except Exception as e:
        logger.warning("[_queue_for_thread] failed: %s", e)
        return {"status": "error", "error": str(e)}


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
    agent's reply. If the target thread is busy, the message is automatically
    queued and delivered when it becomes idle.

    Args:
        thread_id: The session ID to send to (from sessions_list).
        message: The message content to deliver.
        timeout_seconds: Seconds to wait for reply (0 = fire-and-forget).

    Returns:
        JSON with status (accepted/queued/ok/timeout/error), thread_id, run_id,
        and optionally reply.
    """
    api_url = LANGGRAPH_API_URL
    full_message = _wrap_origin_message("sessions-send", message, state)
    try:
        response = httpx.post(
            f"{api_url}/threads/{thread_id}/runs",
            headers={"Content-Type": "application/json"},
            json={
                "assistant_id": "main",
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
    except httpx.HTTPStatusError as e:
        # Thread busy (409 Conflict) — queue for later delivery
        if e.response.status_code == 409:
            result = _queue_for_thread(thread_id, full_message, state)
            return _json.dumps(result)
        return _json.dumps({"status": "error", "error": f"HTTP {e.response.status_code}: {e.response.text}"})
    except httpx.ConnectError as e:
        return _json.dumps({"status": "error", "error": f"Connection failed: {e}"})
    except httpx.TimeoutException:
        return _json.dumps({"status": "error", "error": "Request timed out"})
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)})


@tool
def sessions_spawn(
    message: str,
    session_type: Literal["main", "subagent"] = "subagent",
    timeout_seconds: int = 0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Spawn a non-blocking background session.

    Creates a fresh thread and starts a run. The session runs independently
    and supports back-and-forth conversation via sessions_send. Use for
    longer work you don't need to wait on. Set timeout_seconds > 0 to wait
    for the agent's reply.

    Args:
        message: The message to send to the new session.
        session_type: Type of session to create (default "subagent").
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
                "assistant_id": "main",
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


# ---------------------------------------------------------------------------
# Config management
# ---------------------------------------------------------------------------


@tool
def manage_config(
    action: Literal["get", "patch"],
    key: str = None,
    patch: str = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """View or update user configuration.

    Supports these config keys: user, heartbeat, action_gating, skills, inbound, connections, chat_surfaces.
    Use key parameter to read/write a specific section instead of the full config.

    - **user**: User profile (timezone, expandable). Timezone auto-syncs to USER.md.
    - **connections**: External service integrations you act on behalf of the user (Google, GitHub, Slack, etc.).
      GET returns all available services with enabled/disabled status (live from Composio).
      PATCH to enable starts OAuth flow and returns auth URL. PATCH to disable disconnects.
      Triggers are automatically set up/torn down when connections are enabled/disabled.
    - **chat_surfaces**: Chat platforms where you can chat with the user directly (Slack, Teams, Telegram, Whatsapp).
      GET returns all available surfaces with enabled/disabled status.
      PATCH to enable returns setup instructions. PATCH to disable removes credentials.
    - **inbound**: Inbound event toggles per platform (slack, gmail, outlook, meetings).
    - **heartbeat**: Heartbeat frequency and active hours.
    - **action_gating**: Toggle user approval for write/destructive actions on connections (i.e. external services).
      Per-service toggles (google, github, notion, trello, slack, teams, microsoft, browser).
      PATCH '{"action_gating": {"services": {"github": false}}}' to disable gating for GitHub.
      PATCH '{"action_gating": {"enabled": false}}' to disable all action gating.
    - **skills**: Skill enable/disable — each skill has enabled flag and description.
      All skills are always visible. PATCH '{"skills": {"browser": {"enabled": true}}}' to enable.

    Args:
        action: "get" to read, "patch" to update.
        key: Optional config section to target (e.g. "connections", "heartbeat").
            If omitted, operates on the full config (connections/chat_surfaces excluded).
        patch: JSON string for patch action.
            For user: '{"user": {"timezone": "Europe/London"}}'
            For skills: '{"skills": {"browser": {"enabled": true}}}'
            For inbound: '{"inbound": {"gmail": true}}'
            For action_gating: '{"action_gating": {"services": {"github": false}}}'
            For connections: '{"google": "enabled"}' or '{"slack": "disabled"}'
            For chat_surfaces: '{"slack": "enabled"}' or '{"slack": "disabled"}'
              To complete Slack setup: '{"slack": {"token": "xoxb-...", "signing_secret": "...", "owner_slack_id": "U..."}}'

    Returns:
        Current or updated config/status as JSON.
    """
    from agent.config import apply_config_side_effects, load_config, patch_config

    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return "Error: no sandbox available."

    try:
        # --- Live keys (not stored in config.json) ---
        if key == "connections":
            return _handle_connections(action, patch)
        if key == "inbound":
            return _handle_inbound(action, patch)
        if key == "skills":
            return _handle_skills(action, patch, sandbox_id)
        if key == "chat_surfaces":
            return _handle_chat_surfaces(action, patch)

        # --- File-backed config (user, heartbeat, action_gating) ---
        if action == "get":
            config = load_config(sandbox_id)
            data = config.model_dump(exclude_none=True) or {}
            if key:
                if key not in data:
                    return f"Error: unknown config key '{key}'."
                return _json.dumps({key: data[key]})
            return _json.dumps(data)

        elif action == "patch":
            if not patch:
                return "Error: patch is required for patch action."
            try:
                patch_data = _json.loads(patch)
            except _json.JSONDecodeError as e:
                return f"Error: invalid JSON in patch: {e}"

            new_config = patch_config(sandbox_id, patch_data)
            side_effects = apply_config_side_effects(
                new_config, sandbox_id=sandbox_id, patch=patch_data,
            )
            result = {"config": new_config.model_dump(exclude_none=True)}
            if side_effects:
                result.update(side_effects)
            return _json.dumps(result)

        else:
            return f"Error: unknown action '{action}'."

    except Exception as e:
        logger.warning("[manage_config] %s failed: %s", action, e)
        return f"Error: {action} failed: {e}"


# -- Connections handler (Composio-backed) --

def _handle_connections(action: str, patch_str: str | None) -> str:
    from agent.auth import (
        SERVICE_REGISTRY,
        disconnect_service,
        initiate_service,
        list_connected_services,
    )

    if action == "get":
        connected = list_connected_services()
        connected_names = {s["service"] for s in connected if s.get("status") == "ACTIVE"}
        result = {}
        for svc, cfg in SERVICE_REGISTRY.items():
            result[svc] = {
                "display_name": cfg["display_name"],
                "status": "enabled" if svc in connected_names else "disabled",
            }
        return _json.dumps(result)

    elif action == "patch":
        if not patch_str:
            return "Error: patch is required."
        try:
            patch_data = _json.loads(patch_str)
        except _json.JSONDecodeError as e:
            return f"Error: invalid JSON: {e}"

        results = {}
        for service, desired in patch_data.items():
            if desired == "enabled":
                result = initiate_service(service)
                results[service] = result
            elif desired == "disabled":
                result = disconnect_service(service)
                results[service] = result
            else:
                results[service] = {"error": f"Invalid value '{desired}'. Use 'enabled' or 'disabled'."}
        return _json.dumps(results)

    return f"Error: unknown action '{action}'."


# -- Chat surfaces handler (vault-backed) --

def _handle_chat_surfaces(action: str, patch_str: str | None) -> str:
    import os
    from agent.auth import disconnect_slack_chat_surface, vault_get_secret

    supabase_url = os.environ.get("SUPABASE_URL", "")

    telegram_bot_name = os.environ.get("TELEGRAM_BOT_NAME", "")
    whatsapp_bridge_url = os.environ.get("WHATSAPP_BRIDGE_URL", "")

    CHAT_SURFACES = {
        "slack": {
            "display_name": "Slack (chat with user as yourself)",
            "vault_key": "slack_bot_token",
            "install_url": f"{supabase_url}/functions/v1/slack-oauth/install",
            "disconnect_fn": disconnect_slack_chat_surface,
        },
        "teams": {
            "display_name": "Teams (chat with user as yourself)",
            "vault_key": "teams_bot_app_id",
            "install_url": f"{supabase_url}/functions/v1/teams-bot-oauth/install",
            "disconnect_fn": lambda: _disconnect_teams_chat_surface(),
            "post_setup_instructions": (
                "After completing the OAuth flow, find Mally in Teams: "
                "Apps (left sidebar) > search 'Mally' > click Add. "
                "If Mally doesn't appear, it may need admin approval. "
                "Ask the user to check their Teams admin dashboard at "
                "https://admin.teams.microsoft.com > Teams apps > Manage apps > search 'Mally' "
                "and ensure it's unblocked. If they're not an admin, offer to help them draft "
                "a message to their IT admin requesting approval for the Mally app."
            ),
        },
        "telegram": {
            "display_name": "Telegram (chat with user as yourself)",
            "vault_key": "telegram_owner_chat_id",
            "install_url": f"https://t.me/{telegram_bot_name}" if telegram_bot_name else "",
            "disconnect_fn": lambda: _disconnect_telegram_chat_surface(),
        },
        "whatsapp": {
            "display_name": "WhatsApp (chat with user as yourself)",
            "vault_key": "whatsapp_owner_jid",
            "install_url": f"{whatsapp_bridge_url}/qr" if whatsapp_bridge_url else "",
            "disconnect_fn": lambda: _disconnect_whatsapp_chat_surface(whatsapp_bridge_url),
        },
    }

    if action == "get":
        result = {}
        for name, cfg in CHAT_SURFACES.items():
            token = vault_get_secret(cfg["vault_key"])
            result[name] = {
                "display_name": cfg["display_name"],
                "status": "enabled" if token else "disabled",
            }
        return _json.dumps(result)

    elif action == "patch":
        if not patch_str:
            return "Error: patch is required."
        try:
            patch_data = _json.loads(patch_str)
        except _json.JSONDecodeError as e:
            return f"Error: invalid JSON: {e}"

        results = {}
        for surface, desired in patch_data.items():
            if surface not in CHAT_SURFACES:
                results[surface] = {"error": f"Unknown chat surface '{surface}'. Available: {', '.join(CHAT_SURFACES)}"}
                continue

            cfg = CHAT_SURFACES[surface]
            if desired == "enabled":
                if not cfg.get("install_url"):
                    results[surface] = {"error": f"{cfg['display_name']} is not configured on this instance."}
                else:
                    result_entry = {
                        "status": "setup_required",
                        "install_url": cfg["install_url"],
                        "message": (
                            f"To set up {cfg['display_name']} so you can chat with me there, "
                            f"open this link and follow the prompts. "
                            f"Once done, come back and let me know."
                        ),
                    }
                    if cfg.get("post_setup_instructions"):
                        result_entry["post_setup_instructions"] = cfg["post_setup_instructions"]
                    results[surface] = result_entry
            elif desired == "disabled":
                results[surface] = cfg["disconnect_fn"]()
            else:
                results[surface] = {"error": f"Invalid value '{desired}'. Use 'enabled' or 'disabled'."}
        return _json.dumps(results)

    return f"Error: unknown action '{action}'."


def _disconnect_teams_chat_surface() -> dict:
    from agent.auth import vault_delete_secret
    for key in ["teams_bot_app_id", "teams_bot_app_secret", "teams_bot_tenant_id"]:
        vault_delete_secret(key)
    return {"status": "disconnected", "service": "teams"}


def _disconnect_telegram_chat_surface() -> dict:
    from agent.auth import vault_delete_secret
    for key in ["telegram_owner_chat_id", "telegram_owner_user_id", "telegram_owner_name"]:
        vault_delete_secret(key)
    return {"status": "disconnected", "service": "telegram"}


def _disconnect_whatsapp_chat_surface(bridge_url: str) -> dict:
    import httpx
    # Call bridge disconnect endpoint to logout + clear vault
    if bridge_url:
        try:
            httpx.post(f"{bridge_url}/disconnect", timeout=10)
        except Exception:
            pass
    # Also clean vault directly as fallback
    from agent.auth import vault_delete_secret
    for key in ["whatsapp_auth_state", "whatsapp_owner_jid"]:
        vault_delete_secret(key)
    return {"status": "disconnected", "service": "whatsapp"}


# -- Skills handler (volume-backed) --

def _handle_skills(action: str, patch_str: str | None, sandbox_id: str) -> str:
    from agent.config import SKILLS_REGISTRY, SKILLS_VOLUME_DIR, sync_skill_to_volume

    if action == "get":
        # Scan volume for enabled skills
        import modal
        sandbox = modal.Sandbox.from_id(sandbox_id)
        try:
            process = sandbox.exec("ls", SKILLS_VOLUME_DIR, timeout=5)
            process.wait()
            enabled_dirs = set(process.stdout.read().strip().split())
        except Exception:
            enabled_dirs = set()

        result = {}
        for name, description in SKILLS_REGISTRY.items():
            result[name] = {
                "enabled": name in enabled_dirs,
                "description": description,
            }
        return _json.dumps(result)

    elif action == "patch":
        if not patch_str:
            return "Error: patch is required."
        try:
            patch_data = _json.loads(patch_str)
        except _json.JSONDecodeError as e:
            return f"Error: invalid JSON: {e}"

        results = {}
        for skill_name, desired in patch_data.items():
            if skill_name not in SKILLS_REGISTRY:
                results[skill_name] = {"error": f"Unknown skill '{skill_name}'."}
                continue

            if isinstance(desired, dict):
                enable = desired.get("enabled")
            elif isinstance(desired, bool):
                enable = desired
            else:
                results[skill_name] = {"error": f"Invalid value. Use true/false or {{\"enabled\": true/false}}."}
                continue

            if not isinstance(enable, bool):
                results[skill_name] = {"error": "enabled must be true or false."}
                continue

            results[skill_name] = sync_skill_to_volume(sandbox_id, skill_name, enable)
        return _json.dumps(results)

    return f"Error: unknown action '{action}'."


# -- Inbound handler (Composio triggers + meetings) --

def _handle_inbound(action: str, patch_str: str | None) -> str:
    from agent.auth import (
        TRIGGER_REGISTRY,
        list_connected_services,
        setup_triggers,
        teardown_triggers,
        _list_composio_accounts,
        _find_account_by_slug,
        SERVICE_REGISTRY,
    )
    import httpx

    # Map platform names to services that have Composio triggers
    # slack → slack, gmail → google, outlook → microsoft
    TRIGGER_SOURCES = {
        "slack": "slack",
        "gmail": "google",
        "outlook": "microsoft",
    }

    # Sources managed via Graph API subscriptions
    GRAPH_SOURCES = {"teams"}

    # Sources managed via vault secret (not Composio triggers)
    VAULT_SOURCES = {"meetings"}

    ALL_SOURCES = set(TRIGGER_SOURCES) | GRAPH_SOURCES | VAULT_SOURCES

    def _get_vault_inbound_config(sandbox_id: str | None) -> dict:
        """Read inbound_sources vault secret as JSON dict."""
        try:
            from agent.modal_backend import _get_supabase
            sb = _get_supabase()
            result = sb.rpc("get_vault_secret", {"secret_name": "inbound_sources"}).execute()
            raw = result.data
            if raw:
                return _json.loads(raw)
        except Exception:
            pass
        return {}

    def _set_vault_inbound_config(config: dict) -> None:
        """Write inbound_sources vault secret as JSON."""
        try:
            from agent.modal_backend import _get_supabase
            sb = _get_supabase()
            sb.rpc("upsert_vault_secret", {
                "secret_name": "inbound_sources",
                "secret_value": _json.dumps(config),
            }).execute()
        except Exception as e:
            logger.warning("[manage_config] vault write failed: %s", e)

    if action == "get":
        # Check which Composio triggers are active
        try:
            from agent.auth import _composio_headers, COMPOSIO_API_URL
            resp = httpx.get(
                f"{COMPOSIO_API_URL}/trigger_instances/active",
                headers=_composio_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            active = resp.json()
            if isinstance(active, dict):
                active = active.get("items", [])
        except Exception:
            active = []

        active_slugs = {(t.get("trigger_slug") or "").upper() for t in active}

        result = {}
        for source, service in TRIGGER_SOURCES.items():
            trigger_slugs = TRIGGER_REGISTRY.get(service, [])
            has_active = any(s.upper() in active_slugs for s in trigger_slugs)
            result[source] = {"enabled": has_active}

        # Teams — check for active Graph subscriptions (not Composio triggers)
        teams_enabled = False
        try:
            import os
            supabase_url = os.environ.get("SUPABASE_URL", "")
            subs_resp = httpx.post(
                f"{supabase_url}/functions/v1/teams-subscriptions/list",
                headers={"Authorization": f"Bearer {os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')}"},
                timeout=15,
            )
            if subs_resp.status_code == 200:
                subs_data = subs_resp.json()
                teams_enabled = bool(subs_data.get("subscriptions"))
        except Exception:
            pass
        result["teams"] = {"enabled": teams_enabled}

        # Meetings — read from vault
        vault_config = _get_vault_inbound_config(None)
        meetings_enabled = vault_config.get("meetings", False)
        result["meetings"] = {
            "enabled": meetings_enabled,
            "app_installed": None,  # TODO: detect whether Electron app is installed
        }

        return _json.dumps(result)

    elif action == "patch":
        if not patch_str:
            return "Error: patch is required."
        try:
            patch_data = _json.loads(patch_str)
        except _json.JSONDecodeError as e:
            return f"Error: invalid JSON: {e}"

        results = {}
        for source, desired in patch_data.items():
            if source not in ALL_SOURCES:
                results[source] = {"error": f"Unknown source '{source}'. Available: {', '.join(sorted(ALL_SOURCES))}"}
                continue

            enable = desired if isinstance(desired, bool) else desired.get("enabled") if isinstance(desired, dict) else None
            if not isinstance(enable, bool):
                results[source] = {"error": "Use true or false."}
                continue

            # Teams — toggle via Graph subscriptions (not Composio triggers)
            if source == "teams":
                try:
                    import os
                    supabase_url = os.environ.get("SUPABASE_URL", "")
                    svc_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
                    endpoint = "subscribe" if enable else "unsubscribe"
                    resp = httpx.post(
                        f"{supabase_url}/functions/v1/teams-subscriptions/{endpoint}",
                        headers={"Authorization": f"Bearer {svc_key}"},
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        results[source] = {"enabled": enable}
                    else:
                        results[source] = {"error": f"Teams {endpoint} failed: {resp.status_code} {resp.text[:200]}"}
                except Exception as e:
                    results[source] = {"error": str(e)}
                continue

            # Meetings — toggle via vault secret
            if source == "meetings":
                vault_config = _get_vault_inbound_config(None)
                vault_config["meetings"] = enable
                _set_vault_inbound_config(vault_config)
                result_entry = {
                    "enabled": enable,
                    "app_installed": None,  # TODO: detect whether Electron app is installed; surface DMG/install link if not
                }
                results[source] = result_entry
                continue

            # Teams — toggle via Graph subscriptions
            if source == "teams":
                try:
                    import os
                    supabase_url = os.environ.get("SUPABASE_URL", "")
                    svc_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
                    endpoint = "subscribe" if enable else "unsubscribe"
                    resp = httpx.post(
                        f"{supabase_url}/functions/v1/teams-subscriptions/{endpoint}",
                        headers={"Authorization": f"Bearer {svc_key}"},
                        timeout=30,
                    )
                    if resp.status_code == 200:
                        results[source] = {"enabled": enable}
                    else:
                        results[source] = {"error": f"Teams {endpoint} failed: {resp.status_code} {resp.text[:200]}"}
                except Exception as e:
                    results[source] = {"error": str(e)}
                continue

            # Composio trigger sources (slack, gmail, outlook)
            service = TRIGGER_SOURCES[source]

            if enable:
                try:
                    accounts = _list_composio_accounts()
                    svc_config = SERVICE_REGISTRY[service]
                    acct = _find_account_by_slug(accounts, svc_config["composio_slug"])
                    if not acct or acct.get("status") != "ACTIVE":
                        results[source] = {"error": f"Connection '{service}' must be enabled first."}
                        continue
                    trigger_results = setup_triggers(service, acct["id"])
                    results[source] = {"enabled": True, "triggers": trigger_results}
                except Exception as e:
                    results[source] = {"error": str(e)}
            else:
                trigger_results = teardown_triggers(service)
                results[source] = {"enabled": False, "triggers": trigger_results}

        return _json.dumps(results)

    return f"Error: unknown action '{action}'."


def _is_heartbeat_job(sb, *, job_id: int = None, job_name: str = None) -> bool:
    """Check if a cron job is the heartbeat (managed by config, not user-editable)."""
    if job_name and "heartbeat" in job_name.lower():
        return True
    if job_id is not None:
        try:
            result = sb.rpc("list_agent_crons").execute()
            for job in result.data or []:
                if job.get("jobid") == job_id:
                    return "heartbeat" in (job.get("jobname") or "").lower()
        except Exception:
            pass
    return False


@tool
def manage_crons(
    action: Literal["status", "list", "add", "update", "remove", "run", "runs", "wake"],
    job_name: str = None,
    job_id: int = None,
    schedule: str = None,
    schedule_type: Literal["cron", "at", "every"] = "cron",
    input_message: str = None,
    active: bool = None,
    active_filter: Literal["all", "active", "inactive"] = "active",
    limit: int = 20,
    offset: int = 0,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Manage cron jobs and wake events (use for reminders; when scheduling a reminder, write the input_message as something that will read like a reminder when it fires, and mention that it is a reminder depending on the time gap between setting and firing; include recent context in reminder text if appropriate).

    add: Creates a cron job that posts the input_message to the latest main session
    each time the schedule triggers.
    wake: Trigger an immediate heartbeat. Use when user asks to run a heartbeat.

    Args:
        action: One of: status, list, add, update, remove, run, runs, wake.
        job_name: Name for the cron job (required for add/remove).
        job_id: Job ID (required for update/run/runs).
        schedule: Schedule expression (required for add, optional for update).
            - schedule_type="cron": standard cron expression (e.g., "*/5 * * * *")
            - schedule_type="at": UTC datetime ISO-8601 (e.g., "2026-03-03T17:30:00Z") — fires once then deactivated
            - schedule_type="every": interval string (e.g., "5m", "2h", "1d")
        schedule_type: How to interpret 'schedule': "cron" (default), "at" (one-shot), or "every" (interval).
        input_message: Message sent to agent when cron fires (required for add).
        active: Enable/disable a job (for update).
        active_filter: Filter list results: "active" (default), "inactive", or "all".
        limit: Max number of jobs to return for list action (default 20).
        offset: Pagination offset for list action (default 0).

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
            # Guard: heartbeat cron is managed by config — use manage_config instead
            if _is_heartbeat_job(sb, job_id=job_id):
                return "Error: heartbeat schedule is managed by config. Use manage_config to change heartbeat frequency or active hours."
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
            # Guard: heartbeat cron is managed by config — use manage_config instead
            if _is_heartbeat_job(sb, job_name=job_name):
                return "Error: heartbeat cron is managed by config. Use manage_config to change heartbeat settings."
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
            result = sb.rpc("wake_agent", {}).execute()
            return _json.dumps({"triggered": True, "request_id": result.data})

        else:
            return f"Error: Unknown action '{action}'."

    except Exception as e:
        logger.warning("[manage_crons] %s failed: %s", action, e)
        return f"Error: {action} failed: {e}"


# ---------------------------------------------------------------------------
# Auth management (Composio)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Messaging (Slack, Teams)
# ---------------------------------------------------------------------------

_MSG_AUTH_DIR = "/mnt/auth"

_MSG_TOKEN_FILES: dict[str, str] = {
    "slack": f"{_MSG_AUTH_DIR}/slack_token",
    "teams": f"{_MSG_AUTH_DIR}/teams_token",
}


def _read_token_from_sandbox(sandbox, token_file: str) -> str:
    """Read an auth token file from the sandbox."""
    process = sandbox.exec("cat", token_file, timeout=10)
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read()
        raise RuntimeError(
            f"Token not found at {token_file}. "
            f"Run python3 /mnt/auth/fetch_auth.py first. ({stderr})"
        )
    return process.stdout.read().strip()


def _send_slack(
    token: str, recipient: str, text: str, thread_ts: str | None = None,
) -> dict[str, Any]:
    """Send a message via Slack Web API chat.postMessage."""
    payload: dict[str, Any] = {"channel": recipient, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    resp = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        return {
            "status": "error",
            "platform": "slack",
            "error": data.get("error", "unknown"),
        }

    return {
        "status": "sent",
        "platform": "slack",
        "channel": data.get("channel"),
        "ts": data.get("ts"),
        "message_id": data.get("ts"),
    }


def _send_teams(token: str, recipient: str, text: str) -> dict[str, Any]:
    """Send a message via Microsoft Graph API.

    recipient can be:
      - A chat ID (for 1:1 or group chats)
      - "team:{teamId}/channel:{channelId}" for channel messages
    """
    if recipient.startswith("team:"):
        parts = recipient.split("/channel:")
        team_id = parts[0].removeprefix("team:")
        channel_id = parts[1] if len(parts) > 1 else ""
        if not channel_id:
            return {"status": "error", "platform": "teams", "error": "Invalid recipient format. Use team:{teamId}/channel:{channelId}"}
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"
    else:
        url = f"https://graph.microsoft.com/v1.0/chats/{recipient}/messages"

    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"body": {"content": text}},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "status": "sent",
        "platform": "teams",
        "message_id": data.get("id"),
        "chat_id": recipient,
    }


def _send_teams_bot(recipient: str, text: str) -> dict[str, Any]:
    """Send a message via Bot Framework API (chat surface).

    Uses the bot's app credentials to get a token, then posts to the
    stored serviceUrl conversation endpoint.
    """
    from agent.auth import vault_get_secret

    app_id = vault_get_secret("teams_bot_app_id")
    app_secret = vault_get_secret("teams_bot_app_secret")
    service_url = vault_get_secret("teams_bot_service_url")
    tenant_id = vault_get_secret("teams_bot_tenant_id")

    if not app_id or not app_secret:
        raise RuntimeError("Teams bot credentials not found in vault.")
    if not service_url:
        raise RuntimeError("Teams bot service URL not found. The user needs to message the bot first.")

    # Get Bot Framework token — SingleTenant uses tenant-specific endpoint
    token_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        if tenant_id
        else "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    )
    token_resp = httpx.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": app_secret,
            "scope": "https://api.botframework.com/.default",
        },
        timeout=15,
    )
    token_resp.raise_for_status()
    bot_token = token_resp.json()["access_token"]

    # Send via Bot Framework conversations API
    url = f"{service_url}v3/conversations/{recipient}/activities"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json",
        },
        json={"type": "message", "text": text},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    return {
        "status": "sent",
        "platform": "teams",
        "message_id": data.get("id"),
        "chat_id": recipient,
    }


_MSG_SENDERS: dict[str, callable] = {
    "slack": _send_slack,
    "teams": _send_teams,
}


def _resolve_slack_token(sandbox, via: str | None) -> str:
    """Resolve the Slack token based on via preference.

    Priority: explicit via > chat_surface if token exists > connection OAuth.
    """
    from agent.auth import vault_get_secret, SLACK_BOT_TOKEN_SECRET

    use_chat_surface = via == "chat_surface" or via is None

    if use_chat_surface:
        token = vault_get_secret(SLACK_BOT_TOKEN_SECRET)
        if token:
            return token
        if via == "chat_surface":
            raise RuntimeError('Chat surface token not found. Run manage_config key="chat_surfaces" to set up Slack first.')
        # Fall through to connection token

    # User OAuth token from sandbox (connection)
    return _read_token_from_sandbox(sandbox, _MSG_TOKEN_FILES["slack"])


@tool
def send_message(
    platform: Literal["slack", "teams"],
    recipient: str,
    text: str,
    thread_ts: str = None,
    via: Literal["chat_surface", "connection"] = None,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Send a message to a user or channel on Slack or Microsoft Teams.

    For channel-message sessions (inbound from webhook), use the platform,
    channel, and thread_ts from the system-message tag to reply in context.

    Args:
        platform: Target platform — "slack" or "teams".
        recipient: Who to send to.
            Slack: channel ID (C...), user ID (U...), or channel name (#general).
            Teams: chat ID for 1:1/group chats, or "team:{teamId}/channel:{channelId}" for channels.
        text: Message content (plain text).
        thread_ts: (Slack only) Thread timestamp to reply in-thread. Use the
            thread_ts from the inbound channel-message to keep the conversation
            in the same thread.
        via: How to send the message (Slack and Teams):
            - "chat_surface" — sends as yourself (set up via manage_config key="chat_surfaces").
              This is the default and preferred option.
            - "connection" — sends as the user themselves via their OAuth token.
              **SENSITIVE**: This posts as the actual user, not as yourself. Always get
              explicit user approval before using this option. Never assume the user wants
              messages sent under their name.

    Returns:
        JSON with send status and message details.
    """
    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return _json.dumps({"status": "error", "error": "No sandbox available."})

    # Teams chat surface: use Bot Framework API directly (no sandbox token needed)
    if platform == "teams" and (via == "chat_surface" or via is None):
        try:
            result = _send_teams_bot(recipient, text)
            return _json.dumps(result)
        except Exception as e:
            if via == "chat_surface":
                return _json.dumps({"status": "error", "platform": "teams", "error": str(e)})
            # Fall through to connection token if via was None

    sender_fn = _MSG_SENDERS.get(platform)
    if not sender_fn:
        return _json.dumps({"status": "error", "error": f"Unsupported platform: {platform}"})

    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        if platform == "slack":
            token = _resolve_slack_token(sandbox, via)
        else:
            token_file = _MSG_TOKEN_FILES.get(platform)
            token = _read_token_from_sandbox(sandbox, token_file)
    except Exception as e:
        return _json.dumps({
            "status": "error",
            "error": f"Failed to read {platform} token: {e}. Run python3 /mnt/auth/fetch_auth.py {platform} first.",
        })

    try:
        if platform == "slack" and thread_ts:
            result = sender_fn(token, recipient, text, thread_ts=thread_ts)
        else:
            result = sender_fn(token, recipient, text)
        return _json.dumps(result)
    except httpx.HTTPStatusError as e:
        return _json.dumps({
            "status": "error",
            "platform": platform,
            "error": f"HTTP {e.response.status_code}: {e.response.text[:500]}",
        })
    except Exception as e:
        logger.warning("[send_message] %s send failed: %s", platform, e)
        return _json.dumps({"status": "error", "platform": platform, "error": str(e)})



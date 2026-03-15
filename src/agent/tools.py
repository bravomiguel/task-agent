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
    """Mandatory recall step: semantically search MEMORY.md + memory/*.md before answering questions about prior work, decisions, dates, people, preferences, or todos; returns top snippets with path + lines.

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
        session_type: Filter by session type (e.g. "main", "cron").
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


def _queue_for_thread(thread_id: str, message: str, state: dict | None, source: str = "sessions-send") -> dict:
    """Insert a message into inbound_queue for later delivery to a specific thread.

    Used as fallback when a thread is busy and can't accept a run immediately.
    """
    session_type = state.get("session_type", "unknown") if state else "unknown"
    priority_map = {"subagent": 3, "cron": 4, "heartbeat": 5}
    priority = priority_map.get(session_type, 3)

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

    Supports these config keys: timezone, heartbeat, skills, channels, connections, chat_surfaces.
    Use key parameter to read/write a specific section instead of the full config.

    - **connections**: External service integrations (Google, GitHub, Slack, etc.).
      GET returns all available services with enabled/disabled status (live from Composio).
      PATCH to enable starts OAuth flow and returns auth URL. PATCH to disable disconnects.
    - **chat_surfaces**: Chat platforms where the user can chat with you (Slack, Teams, etc.).
      GET returns all available surfaces with enabled/disabled status.
      PATCH to enable returns setup instructions. PATCH to disable removes credentials.
    - **channels**: Inbound event toggles per platform (slack, teams, gmail, outlook).
    - **heartbeat**: Heartbeat frequency and active hours.
    - **skills**: Skill enable/disable with descriptions.
    - **timezone**: IANA timezone string (auto-syncs to USER.md).

    Args:
        action: "get" to read, "patch" to update.
        key: Optional config section to target (e.g. "connections", "heartbeat").
            If omitted, operates on the full config (connections/chat_surfaces excluded).
        patch: JSON string for patch action.
            For connections: '{"google": "enabled"}' or '{"slack": "disabled"}'
            For chat_surfaces: '{"slack": "enabled"}' or '{"slack": "disabled"}'
              To complete Slack setup: '{"slack": {"token": "xoxb-...", "signing_secret": "...", "owner_slack_id": "U..."}}'
            For other keys: standard config merge patch.

    Returns:
        Current or updated config/status as JSON.
    """
    from agent.config import apply_config_side_effects, load_config, patch_config

    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return "Error: no sandbox available."

    try:
        # --- Connections (live from Composio, not stored in config.json) ---
        if key == "connections":
            return _handle_connections(action, patch)

        # --- Chat surfaces (vault-backed, not stored in config.json) ---
        if key == "chat_surfaces":
            return _handle_chat_surfaces(action, patch)

        # --- Standard config keys (file-backed) ---
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

_CHAT_SURFACE_REGISTRY = {
    "slack": {"display_name": "Slack"},
}


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
    from agent.auth import (
        connect_slack_bot,
        disconnect_slack_bot,
        vault_get_secret,
    )

    if action == "get":
        result = {}
        # Slack bot
        bot_token = vault_get_secret("slack_bot_token")
        result["slack"] = {
            "display_name": "Slack",
            "status": "enabled" if bot_token else "disabled",
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
            if surface == "slack":
                if isinstance(desired, dict):
                    # Phase 2: credentials provided
                    result = connect_slack_bot(
                        token=desired.get("token"),
                        signing_secret=desired.get("signing_secret"),
                        owner_slack_id=desired.get("owner_slack_id"),
                    )
                    results[surface] = result
                elif desired == "enabled":
                    # Phase 1: return setup instructions
                    result = connect_slack_bot(token=None)
                    results[surface] = result
                elif desired == "disabled":
                    result = disconnect_slack_bot()
                    results[surface] = result
                else:
                    results[surface] = {"error": f"Invalid value '{desired}'."}
            else:
                results[surface] = {"error": f"Unknown chat surface '{surface}'."}
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


@tool
def fetch_auth(
    service: str,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Fetch OAuth credentials for a connected service into the sandbox.

    Call this when a skill needs fresh credentials for an already-connected service.
    Use manage_config with key="connections" to view/enable/disable connections.

    Args:
        service: Service name (e.g. "google", "github", "slack", "teams").

    Returns:
        JSON with token file path and usage instructions.
    """
    from agent.auth import connect_service

    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return "Error: no sandbox available."

    try:
        result = connect_service(service, sandbox_id)
        return _json.dumps(result)
    except Exception as e:
        logger.warning("[fetch_auth] %s failed: %s", service, e)
        return f"Error: fetch_auth for {service} failed: {e}"


# ---------------------------------------------------------------------------
# Messaging (Slack, Teams)
# ---------------------------------------------------------------------------

_MSG_AUTH_DIR = "/workspace/.auth"

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
            f"Run fetch_auth first. ({stderr})"
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
        via: (Slack only) How to send the message:
            - "chat_surface" — sends as the assistant app (set up via manage_config key="chat_surfaces").
              This is the default and preferred option.
            - "connection" — sends as the user themselves via their OAuth token.
              **SENSITIVE**: This posts as the actual user, not the assistant. Always get
              explicit user approval before using this option. Never assume the user wants
              messages sent under their name.

    Returns:
        JSON with send status and message details.
    """
    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return _json.dumps({"status": "error", "error": "No sandbox available."})

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
            "error": f"Failed to read {platform} token: {e}. Run fetch_auth service=\"{platform}\" first.",
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



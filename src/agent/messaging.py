"""messaging.py — Send messages to external platforms (Slack, Teams, etc.).

Provides a `send_message` tool that reads auth tokens from the sandbox
and delivers messages via platform APIs.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Annotated, Any, Literal

import httpx
import modal
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger(__name__)

AUTH_DIR = "/workspace/.auth"


# ---------------------------------------------------------------------------
# Sandbox helpers
# ---------------------------------------------------------------------------


def _read_token_from_sandbox(sandbox, token_file: str) -> str:
    """Read an auth token file from the sandbox."""
    process = sandbox.exec("cat", token_file, timeout=10)
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read()
        raise RuntimeError(
            f"Token not found at {token_file}. "
            f"Run manage_auth connect first. ({stderr})"
        )
    return process.stdout.read().strip()


# ---------------------------------------------------------------------------
# Platform adapters
# ---------------------------------------------------------------------------


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
        # Channel message: team:{teamId}/channel:{channelId}
        parts = recipient.split("/channel:")
        team_id = parts[0].removeprefix("team:")
        channel_id = parts[1] if len(parts) > 1 else ""
        if not channel_id:
            return {"status": "error", "platform": "teams", "error": "Invalid recipient format. Use team:{teamId}/channel:{channelId}"}
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"
    else:
        # 1:1 or group chat
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


_PLATFORM_TOKEN_FILES: dict[str, str] = {
    "slack": f"{AUTH_DIR}/slack_token",
    "teams": f"{AUTH_DIR}/teams_token",
}

_PLATFORM_SENDERS: dict[str, callable] = {
    "slack": _send_slack,
    "teams": _send_teams,
}


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@tool
def send_message(
    platform: Literal["slack", "teams"],
    recipient: str,
    text: str,
    thread_ts: str = None,
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

    Returns:
        JSON with send status and message details.
    """
    sandbox_id = state.get("modal_sandbox_id") if state else None
    if not sandbox_id:
        return _json.dumps({"status": "error", "error": "No sandbox available."})

    token_file = _PLATFORM_TOKEN_FILES.get(platform)
    sender_fn = _PLATFORM_SENDERS.get(platform)
    if not token_file or not sender_fn:
        return _json.dumps({"status": "error", "error": f"Unsupported platform: {platform}"})

    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        token = _read_token_from_sandbox(sandbox, token_file)
    except Exception as e:
        return _json.dumps({
            "status": "error",
            "error": f"Failed to read {platform} token: {e}. Run manage_auth connect {platform} first.",
        })

    try:
        # Pass thread_ts for Slack thread replies
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

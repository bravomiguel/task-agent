"""Read Claude Code CLI OAuth credentials for Anthropic API authentication."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _read_from_keychain() -> str | None:
    """Try reading access token from macOS keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        return data.get("claudeAiOauth", {}).get("accessToken") or None
    except Exception:
        return None


def _read_from_file() -> str | None:
    """Try reading access token from ~/.claude/.credentials.json."""
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken") or None
    except Exception:
        return None


def get_claude_code_token() -> str:
    """Return the Claude Code OAuth access token.

    Checks macOS keychain first, then falls back to the credentials file.
    Raises RuntimeError if no valid token is found.
    """
    token = _read_from_keychain() or _read_from_file()
    if not token:
        raise RuntimeError(
            "No Claude Code OAuth token found. "
            "Ensure Claude Code is authenticated "
            "(check macOS keychain or ~/.claude/.credentials.json)."
        )
    return token

"""Read Codex CLI OAuth credentials for OpenAI API authentication via openai-oauth proxy."""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

_AUTH_PATHS = [
    Path.home() / ".chatgpt-local" / "auth.json",
    Path.home() / ".codex" / "auth.json",
]
_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_EXPIRY_BUFFER_S = 300  # refresh 5 min before expiry


def _find_auth_file() -> Path | None:
    """Find the first existing Codex auth file."""
    # Check env var overrides first
    import os

    for env_var in ("CHATGPT_LOCAL_HOME", "CODEX_HOME"):
        home = os.environ.get(env_var)
        if home:
            p = Path(home) / "auth.json"
            if p.exists():
                return p
    # Default paths
    for p in _AUTH_PATHS:
        if p.exists():
            return p
    return None


def _read_credentials() -> tuple[dict | None, Path | None]:
    """Read Codex OAuth credentials. Returns (creds_dict, auth_file_path)."""
    auth_file = _find_auth_file()
    if not auth_file:
        return None, None
    try:
        data = json.loads(auth_file.read_text())
        tokens = data.get("tokens", {})
        if tokens.get("access_token"):
            return data, auth_file
    except Exception:
        pass
    return None, None


def _is_expired(data: dict) -> bool:
    """Check if the token needs refresh."""
    last_refresh = data.get("last_refresh")
    if not last_refresh:
        return True
    # Access tokens typically expire in 1 hour; refresh 5 min early
    try:
        from datetime import datetime

        refreshed_at = datetime.fromisoformat(last_refresh.replace("Z", "+00:00"))
        elapsed = (datetime.now(refreshed_at.tzinfo) - refreshed_at).total_seconds()
        return elapsed > (3600 - _EXPIRY_BUFFER_S)
    except Exception:
        return True


def _refresh_token(refresh_token: str) -> dict:
    """Exchange refresh token for a new access token."""
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "codex-cli/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _save_credentials(auth_file: Path, data: dict, new_tokens: dict) -> None:
    """Update credentials file with refreshed tokens."""
    try:
        data["tokens"]["access_token"] = new_tokens["access_token"]
        if "refresh_token" in new_tokens:
            data["tokens"]["refresh_token"] = new_tokens["refresh_token"]
        if "id_token" in new_tokens:
            data["tokens"]["id_token"] = new_tokens["id_token"]
        data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        auth_file.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def get_codex_token() -> str:
    """Return a valid Codex OAuth access token.

    Reads credentials from ~/.codex/auth.json (or CODEX_HOME).
    If expired, refreshes automatically using the refresh token.
    """
    data, auth_file = _read_credentials()
    if not data:
        raise RuntimeError(
            "No Codex OAuth token found. "
            "Run `codex login` to authenticate, "
            "or check ~/.codex/auth.json."
        )

    tokens = data.get("tokens", {})

    if _is_expired(data):
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError(
                "Codex OAuth token expired and no refresh token available. "
                "Run `codex login` to re-authenticate."
            )
        new_tokens = _refresh_token(refresh_token)
        _save_credentials(auth_file, data, new_tokens)
        return new_tokens["access_token"]

    return tokens["access_token"]

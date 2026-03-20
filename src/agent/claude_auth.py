"""Read Claude Code CLI OAuth credentials for Anthropic API authentication."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_EXPIRY_BUFFER_S = 300  # refresh 5 min before expiry


def _read_credentials() -> dict | None:
    """Read the full claudeAiOauth object from keychain or file."""
    for reader in (_read_creds_from_keychain, _read_creds_from_file):
        creds = reader()
        if creds and creds.get("accessToken"):
            return creds
    return None


def _read_creds_from_keychain() -> dict | None:
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
        return data.get("claudeAiOauth") or None
    except Exception:
        return None


def _read_creds_from_file() -> dict | None:
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text())
        return data.get("claudeAiOauth") or None
    except Exception:
        return None


def _is_expired(creds: dict) -> bool:
    expires_at = creds.get("expiresAt", 0)
    # expiresAt is in milliseconds
    return (time.time() * 1000) >= (expires_at - _EXPIRY_BUFFER_S * 1000)


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
            "User-Agent": "claude-code/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _save_credentials(access_token: str, refresh_token: str, expires_in: int) -> None:
    """Update credentials in keychain and file."""
    expires_at = int(time.time() * 1000) + expires_in * 1000

    # Update file
    try:
        data = json.loads(_CREDENTIALS_PATH.read_text()) if _CREDENTIALS_PATH.exists() else {}
        oauth = data.setdefault("claudeAiOauth", {})
        oauth["accessToken"] = access_token
        oauth["refreshToken"] = refresh_token
        oauth["expiresAt"] = expires_at
        _CREDENTIALS_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

    # Update keychain
    try:
        data_from_keychain = _read_keychain_raw()
        if data_from_keychain is not None:
            oauth = data_from_keychain.setdefault("claudeAiOauth", {})
            oauth["accessToken"] = access_token
            oauth["refreshToken"] = refresh_token
            oauth["expiresAt"] = expires_at
            payload = json.dumps(data_from_keychain)
            # Delete then re-add (macOS keychain doesn't support in-place update)
            subprocess.run(
                ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["security", "add-generic-password", "-s", _KEYCHAIN_SERVICE,
                 "-a", _KEYCHAIN_SERVICE, "-w", payload],
                capture_output=True, timeout=5, check=True,
            )
    except Exception:
        pass


def _read_keychain_raw() -> dict | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout.strip())
    except Exception:
        return None


def get_claude_code_token() -> str:
    """Return a valid Claude Code OAuth access token.

    Reads credentials from macOS keychain or file. If expired, refreshes
    automatically using the refresh token and persists the new credentials.
    """
    creds = _read_credentials()
    if not creds:
        raise RuntimeError(
            "No Claude Code OAuth token found. "
            "Ensure Claude Code is authenticated "
            "(check macOS keychain or ~/.claude/.credentials.json)."
        )

    if _is_expired(creds):
        refresh_token = creds.get("refreshToken")
        if not refresh_token:
            raise RuntimeError(
                "Claude Code OAuth token expired and no refresh token available. "
                "Run `claude` to re-authenticate."
            )
        resp = _refresh_token(refresh_token)
        _save_credentials(
            access_token=resp["access_token"],
            refresh_token=resp.get("refresh_token", refresh_token),
            expires_in=resp["expires_in"],
        )
        return resp["access_token"]

    return creds["accessToken"]

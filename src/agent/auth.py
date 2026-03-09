"""auth.py — Composio auth integration for external service credentials.

Fetches OAuth tokens from Composio via REST API and writes them into the
Modal sandbox for CLI tools (gog, gh, etc.) to use.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import modal

logger = logging.getLogger(__name__)

COMPOSIO_API_URL = "https://backend.composio.dev/api/v3"
AUTH_DIR = "/workspace/.auth"

# ---------------------------------------------------------------------------
# Service registry — extend to add new services
# ---------------------------------------------------------------------------

SERVICE_REGISTRY: dict[str, dict[str, Any]] = {
    "google": {
        "display_name": "Google Workspace",
        "composio_slug": "googlesuper",
        "auth_config_id": "ac_B08Vw1Ia0L05",
        "token_file": f"{AUTH_DIR}/google_token",
    },
    "github": {
        "display_name": "GitHub",
        "composio_slug": "github",
        "auth_config_id": "ac_slOJlN3EhyrJ",
        "token_file": f"{AUTH_DIR}/github_token",
    },
    "notion": {
        "display_name": "Notion",
        "composio_slug": "notion",
        "auth_config_id": "ac_pWHzlMJkhfn7",
        "token_file": f"{AUTH_DIR}/notion_token",
    },
    "trello": {
        "display_name": "Trello",
        "composio_slug": "trello",
        "auth_config_id": "ac_1h8eZlEgyiqz",
        "token_file": f"{AUTH_DIR}/trello_token",
        "key_file": f"{AUTH_DIR}/trello_key",
    },
    "slack": {
        "display_name": "Slack",
        "composio_slug": "slack",
        "auth_config_id": "ac_gB4gJrTfl1Rh",
        "token_file": f"{AUTH_DIR}/slack_token",
    },
    "teams": {
        "display_name": "Microsoft Teams",
        "composio_slug": "microsoft_teams",
        "auth_config_id": "ac_PLACEHOLDER_TEAMS",  # TODO: create in Composio dashboard
        "token_file": f"{AUTH_DIR}/teams_token",
    },
    "microsoft": {
        "display_name": "Microsoft 365 (Outlook)",
        "composio_slug": "outlook",
        "auth_config_id": "ac_PLACEHOLDER_MS365",  # TODO: create in Composio dashboard
        "token_file": f"{AUTH_DIR}/microsoft_token",
    },
}

# Reverse map: composio toolkit slug → our service name
_SLUG_TO_SERVICE: dict[str, str] = {}
for _svc, _cfg in SERVICE_REGISTRY.items():
    _SLUG_TO_SERVICE[_cfg["composio_slug"]] = _svc


# ---------------------------------------------------------------------------
# Composio REST API helpers
# ---------------------------------------------------------------------------


def _composio_headers() -> dict[str, str]:
    api_key = os.environ.get("COMPOSIO_API_KEY")
    if not api_key:
        raise RuntimeError("COMPOSIO_API_KEY environment variable not set")
    return {"x-api-key": api_key}


def _composio_entity_id() -> str:
    return os.environ.get("COMPOSIO_ENTITY_ID", "default")


def _list_composio_accounts(
    *, status: str = "ACTIVE",
) -> list[dict[str, Any]]:
    """List connected accounts from Composio REST API."""
    params: dict[str, Any] = {"statuses": status}
    entity_id = _composio_entity_id()
    if entity_id != "default":
        params["user_ids"] = entity_id

    resp = httpx.get(
        f"{COMPOSIO_API_URL}/connected_accounts",
        headers=_composio_headers(),
        params=params,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def _find_account_by_slug(
    accounts: list[dict], slug: str,
) -> dict | None:
    """Find a connected account by toolkit slug."""
    for acct in accounts:
        toolkit = acct.get("toolkit", {})
        acct_slug = toolkit.get("slug") if isinstance(toolkit, dict) else toolkit
        if acct_slug == slug:
            return acct
    return None


# ---------------------------------------------------------------------------
# Sandbox token helpers
# ---------------------------------------------------------------------------


def _write_token_to_sandbox(sandbox, token: str, path: str) -> None:
    """Write an auth token to a file in the sandbox with restricted permissions."""
    dir_path = os.path.dirname(path)
    process = sandbox.exec(
        "bash", "-c",
        f"mkdir -p {dir_path} && cat > {path} && chmod 600 {path}",
        timeout=10,
    )
    process.stdin.write(token.encode())
    process.stdin.write_eof()
    process.stdin.drain()
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read()
        raise RuntimeError(f"Failed to write token to {path}: {stderr}")


# ---------------------------------------------------------------------------
# Bootstrap functions
# ---------------------------------------------------------------------------


def _extract_access_token(acct: dict) -> str | None:
    """Extract access_token from a Composio connected account dict."""
    token = acct.get("state", {}).get("val", {}).get("access_token")
    if not token:
        token = acct.get("data", {}).get("access_token")
    return token


def _bootstrap_google(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Google Workspace auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["google"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "google",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use in gog commands:\n"
            f"  export GOG_ACCESS_TOKEN=$(cat {token_file})\n"
            f"  gog gmail search 'newer_than:7d' --max 10"
        ),
    }


def _bootstrap_github(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap GitHub auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["github"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "github",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use with gh CLI:\n"
            f"  export GH_TOKEN=$(cat {token_file})\n"
            f"  gh repo list --limit 5"
        ),
    }


def _bootstrap_notion(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Notion auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["notion"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "notion",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use with Notion API:\n"
            f"  NOTION_KEY=$(cat {token_file})\n"
            f'  curl -H "Authorization: Bearer $NOTION_KEY" '
            f'-H "Notion-Version: 2025-09-03" '
            f'"https://api.notion.com/v1/search" -X POST -d \'{{}}\''
        ),
    }


def _extract_consumer_key(acct: dict) -> str | None:
    """Extract consumer_key (API key) from a Composio OAuth1 connected account."""
    state_val = acct.get("state", {}).get("val", {})
    # OAuth1 accounts may store consumer_key in state
    for key in ("consumer_key", "key", "api_key"):
        if state_val.get(key):
            return state_val[key]
    # Also check top-level data
    data = acct.get("data", {})
    for key in ("consumer_key", "key", "api_key"):
        if data.get(key):
            return data[key]
    return None


def _bootstrap_trello(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Trello auth in the sandbox.

    Trello uses OAuth1 but accepts simple key+token query params.
    We need both the consumer_key (API key) and the OAuth access_token.
    """
    token = _extract_access_token(acct)
    if not token:
        # OAuth1 may use 'oauth_token' instead of 'access_token'
        state_val = acct.get("state", {}).get("val", {})
        token = state_val.get("oauth_token") or acct.get("data", {}).get("oauth_token")
    if not token:
        return {"error": "No access_token or oauth_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    consumer_key = _extract_consumer_key(acct)
    if not consumer_key:
        return {
            "error": (
                "No consumer_key (API key) found in Trello credentials. "
                "The Composio connected account may not include it. "
                "Check the connected account state in Composio dashboard."
            ),
        }

    token_file = SERVICE_REGISTRY["trello"]["token_file"]
    key_file = SERVICE_REGISTRY["trello"]["key_file"]
    _write_token_to_sandbox(sandbox, token, token_file)
    _write_token_to_sandbox(sandbox, consumer_key, key_file)

    return {
        "status": "connected",
        "service": "trello",
        "token_file": token_file,
        "key_file": key_file,
        "usage": (
            f"Credentials written. Use with Trello API:\n"
            f"  TRELLO_KEY=$(cat {key_file})\n"
            f"  TRELLO_TOKEN=$(cat {token_file})\n"
            f'  curl -s "https://api.trello.com/1/members/me?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq'
        ),
    }


def _bootstrap_slack(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Slack auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["slack"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "slack",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use with Slack Web API:\n"
            f"  export SLACK_TOKEN=$(cat {token_file})\n"
            f'  curl -H "Authorization: Bearer $SLACK_TOKEN" '
            f"https://slack.com/api/auth.test"
        ),
    }


def _bootstrap_teams(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Microsoft Teams auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["teams"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "teams",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use with Microsoft Graph API:\n"
            f"  export TEAMS_TOKEN=$(cat {token_file})\n"
            f'  curl -H "Authorization: Bearer $TEAMS_TOKEN" '
            f"https://graph.microsoft.com/v1.0/me"
        ),
    }


def _bootstrap_microsoft(sandbox, acct: dict) -> dict[str, Any]:
    """Bootstrap Microsoft 365 (Outlook) auth in the sandbox."""
    token = _extract_access_token(acct)
    if not token:
        return {"error": "No access_token in credentials. Check Composio connection status."}

    if token.endswith("..."):
        return {
            "error": "Access token is masked. Disable secret masking in "
                     "Composio project settings (Settings > Project Configuration).",
        }

    token_file = SERVICE_REGISTRY["microsoft"]["token_file"]
    _write_token_to_sandbox(sandbox, token, token_file)

    return {
        "status": "connected",
        "service": "microsoft",
        "token_file": token_file,
        "usage": (
            f"Token written to {token_file}. Use with ms365_cli.py:\n"
            f"  python3 /mnt/skills/microsoft/scripts/ms365_cli.py user\n"
            f"  python3 /mnt/skills/microsoft/scripts/ms365_cli.py mail list --top 5"
        ),
    }


_BOOTSTRAP_FUNCTIONS = {
    "google": _bootstrap_google,
    "github": _bootstrap_github,
    "notion": _bootstrap_notion,
    "trello": _bootstrap_trello,
    "slack": _bootstrap_slack,
    "teams": _bootstrap_teams,
    "microsoft": _bootstrap_microsoft,
}


# ---------------------------------------------------------------------------
# Supabase vault helpers
# ---------------------------------------------------------------------------


def _get_supabase_client():
    """Lazy import to avoid circular dependency with tools.py."""
    from agent.tools import _get_supabase
    return _get_supabase()


def vault_set_secret(name: str, value: str) -> None:
    """Store a secret in Supabase vault (upsert)."""
    sb = _get_supabase_client()
    sb.rpc("set_vault_secret", {"p_name": name, "p_secret": value}).execute()


def vault_get_secret(name: str) -> str | None:
    """Read a secret from Supabase vault."""
    sb = _get_supabase_client()
    result = sb.rpc("get_vault_secret", {"p_name": name}).execute()
    return result.data if result.data else None


def vault_delete_secret(name: str) -> None:
    """Remove a secret from Supabase vault."""
    sb = _get_supabase_client()
    sb.rpc("delete_vault_secret", {"p_name": name}).execute()


# ---------------------------------------------------------------------------
# Slack Bot setup
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN_SECRET = "slack_bot_token"


def _build_slack_manifest(webhook_url: str, app_name: str = "AI Assistant") -> str:
    """Generate a Slack App manifest YAML with the webhook URL pre-filled."""
    return f"""display_information:
  name: "{app_name}"
  description: "Personal AI assistant bot"
features:
  bot_user:
    display_name: "{app_name}"
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write
      - channels:read
      - channels:history
      - groups:read
      - groups:history
      - im:read
      - im:history
      - mpim:read
      - mpim:history
      - app_mentions:read
      - users:read
      - reactions:write
settings:
  event_subscriptions:
    request_url: "{webhook_url}"
    bot_events:
      - message.im
      - message.channels
      - message.groups
      - message.mpim
      - app_mention
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false"""


def connect_slack_bot(token: str | None) -> dict[str, Any]:
    """Handle the Slack bot connection flow.

    Phase 1 (token=None): Return manifest + setup instructions.
    Phase 2 (token provided): Verify token, store in vault.
    """
    import os
    supabase_url = os.environ.get("SUPABASE_URL", "")
    webhook_url = f"{supabase_url}/functions/v1/channel-webhook/slack-bot"

    if not token:
        manifest = _build_slack_manifest(webhook_url)
        return {
            "status": "setup_required",
            "message": (
                "To set up the Slack bot:\n\n"
                "1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**\n"
                "2. Select your workspace\n"
                "3. Switch to YAML tab and paste this manifest:\n\n"
                f"```yaml\n{manifest}\n```\n\n"
                "4. Click **Create** → **Install to Workspace** → **Allow**\n"
                "5. Go to **OAuth & Permissions** and copy the **Bot User OAuth Token** (starts with `xoxb-`)\n"
                "6. Give me the token and I'll finish the setup."
            ),
            "manifest": manifest,
            "webhook_url": webhook_url,
        }

    # Phase 2: verify and store
    if not token.startswith("xoxb-"):
        return {"status": "error", "error": "Invalid token format. Bot tokens start with xoxb-"}

    # Verify via auth.test
    resp = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("ok"):
        return {"status": "error", "error": f"Token verification failed: {data.get('error', 'unknown')}"}

    bot_user_id = data.get("user_id", "")

    # Store token and bot_user_id in vault (edge functions + send_message read both)
    vault_set_secret(SLACK_BOT_TOKEN_SECRET, token)
    vault_set_secret("slack_bot_user_id", bot_user_id)

    return {
        "status": "connected",
        "service": "slack-bot",
        "bot_user_id": bot_user_id,
        "team": data.get("team"),
        "bot_name": data.get("user"),
        "message": (
            f"Slack bot connected! Bot user: {data.get('user')} ({bot_user_id}), "
            f"workspace: {data.get('team')}. "
            f"Users can now DM the bot directly, or add it to channels with /invite @{data.get('user')}."
        ),
    }


def disconnect_slack_bot() -> dict[str, Any]:
    """Remove Slack bot token from vault."""
    vault_delete_secret(SLACK_BOT_TOKEN_SECRET)
    vault_delete_secret("slack_bot_user_id")
    return {"status": "disconnected", "service": "slack-bot"}


def slack_status() -> dict[str, Any]:
    """Return combined Slack integration status (bot + user OAuth)."""
    # Bot status (vault)
    bot_token = vault_get_secret(SLACK_BOT_TOKEN_SECRET)
    bot_user_id = vault_get_secret("slack_bot_user_id")

    # User OAuth status (Composio)
    user_connected = False
    try:
        accounts = _list_composio_accounts()
        acct = _find_account_by_slug(accounts, SERVICE_REGISTRY["slack"]["composio_slug"])
        user_connected = acct is not None and acct.get("status") == "ACTIVE"
    except Exception:
        pass

    return {
        "bot": {
            "connected": bool(bot_token),
            "bot_user_id": bot_user_id,
        },
        "user_oauth": {
            "connected": user_connected,
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_connected_services() -> list[dict[str, Any]]:
    """List services the user has connected via Composio."""
    accounts = _list_composio_accounts()

    results = []
    seen_slugs = set()

    for acct in accounts:
        slug = acct.get("toolkit", {}).get("slug", "")
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        svc_name = _SLUG_TO_SERVICE.get(slug)
        results.append({
            "service": svc_name,
            "display_name": (
                SERVICE_REGISTRY[svc_name]["display_name"]
                if svc_name else slug
            ),
            "composio_slug": slug,
            "status": acct.get("status"),
        })

    return results


def initiate_service(service: str) -> dict[str, Any]:
    """Start an OAuth flow for a service via Composio.

    Returns a redirect URL the user must open in their browser to complete
    authentication. After the user finishes, call connect_service to verify
    and bootstrap the token.
    """
    if service not in SERVICE_REGISTRY:
        available = ", ".join(SERVICE_REGISTRY.keys())
        return {"error": f"Unknown service: {service!r}. Available: {available}"}

    svc_config = SERVICE_REGISTRY[service]
    auth_config_id = svc_config.get("auth_config_id")
    if not auth_config_id:
        return {"error": f"No auth_config_id configured for {service}."}

    resp = httpx.post(
        f"{COMPOSIO_API_URL}/connected_accounts",
        headers={**_composio_headers(), "Content-Type": "application/json"},
        json={
            "auth_config": {"id": auth_config_id},
            "connection": {"user_id": _composio_entity_id()},
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    redirect_url = data.get("redirect_url")
    connection_id = data.get("id")

    if not redirect_url:
        return {"error": "Composio did not return a redirect URL.", "raw": data}

    return {
        "status": "initiated",
        "service": service,
        "display_name": svc_config["display_name"],
        "auth_url": redirect_url,
        "connection_id": connection_id,
        "message": (
            f"Open this link to connect {svc_config['display_name']}:\n"
            f"{redirect_url}\n\n"
            f"After completing sign-in, tell me and I'll finish the setup."
        ),
    }


def connect_service(service: str, sandbox_id: str) -> dict[str, Any]:
    """Fetch fresh credentials from Composio and bootstrap a service in sandbox."""
    if service not in SERVICE_REGISTRY:
        available = ", ".join(SERVICE_REGISTRY.keys())
        return {"error": f"Unknown service: {service!r}. Available: {available}"}

    svc_config = SERVICE_REGISTRY[service]
    accounts = _list_composio_accounts()

    acct = _find_account_by_slug(accounts, svc_config["composio_slug"])
    if not acct or acct.get("status") != "ACTIVE":
        return {
            "error": (
                f"No active {svc_config['display_name']} connection found in Composio. "
                f"User needs to connect via Composio dashboard first."
            ),
        }

    sandbox = modal.Sandbox.from_id(sandbox_id)
    bootstrap_fn = _BOOTSTRAP_FUNCTIONS.get(service)
    if not bootstrap_fn:
        return {"error": f"No bootstrap function for service: {service}"}

    return bootstrap_fn(sandbox, acct)

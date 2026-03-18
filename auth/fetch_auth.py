#!/usr/bin/env python3
"""Fetch OAuth credentials from Composio for a connected service.

Writes access tokens to /mnt/auth/ for CLI tools to use.
Run inside the sandbox before using a service's CLI tools.

Usage:
  python3 /mnt/auth/fetch_auth.py <service>
  python3 /mnt/auth/fetch_auth.py google
  python3 /mnt/auth/fetch_auth.py github

Requires COMPOSIO_API_KEY and COMPOSIO_ENTITY_ID env vars.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

COMPOSIO_API_URL = "https://backend.composio.dev/api/v3"
AUTH_DIR = "/mnt/auth"

SERVICE_REGISTRY = {
    "google": {"slug": "googlesuper", "token_file": f"{AUTH_DIR}/google_token"},
    "github": {"slug": "github", "token_file": f"{AUTH_DIR}/github_token"},
    "notion": {"slug": "notion", "token_file": f"{AUTH_DIR}/notion_token"},
    "trello": {
        "slug": "trello",
        "token_file": f"{AUTH_DIR}/trello_token",
        "key_file": f"{AUTH_DIR}/trello_key",
    },
    "slack": {"slug": "slack", "token_file": f"{AUTH_DIR}/slack_token"},
    "teams": {"slug": "microsoft_teams", "token_file": f"{AUTH_DIR}/teams_token"},
    "microsoft": {"slug": "outlook", "token_file": f"{AUTH_DIR}/microsoft_token"},
    "dropbox": {"slug": "dropbox", "token_file": f"{AUTH_DIR}/dropbox_token"},
    "box": {"slug": "box", "token_file": f"{AUTH_DIR}/box_token"},
}


def _api_get(path, params=None):
    api_key = os.environ.get("COMPOSIO_API_KEY", "")
    url = f"{COMPOSIO_API_URL}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "fetch-auth/1.0",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _find_account(slug):
    entity_id = os.environ.get("COMPOSIO_ENTITY_ID", "default")
    params = {"statuses": "ACTIVE"}
    if entity_id != "default":
        params["user_ids"] = entity_id
    data = _api_get("connected_accounts", params)
    for acct in data.get("items", []):
        toolkit = acct.get("toolkit", {})
        acct_slug = toolkit.get("slug") if isinstance(toolkit, dict) else toolkit
        if acct_slug == slug:
            return acct
    return None


def _extract_token(acct):
    token = acct.get("state", {}).get("val", {}).get("access_token")
    if not token:
        token = acct.get("data", {}).get("access_token")
    return token


def _write_token(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value)
    os.chmod(path, 0o600)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <service>")
        print(f"Available: {', '.join(SERVICE_REGISTRY)}")
        sys.exit(1)

    service = sys.argv[1]
    if service not in SERVICE_REGISTRY:
        print(json.dumps({"error": f"Unknown service: {service}", "available": list(SERVICE_REGISTRY)}))
        sys.exit(1)

    svc = SERVICE_REGISTRY[service]
    acct = _find_account(svc["slug"])
    if not acct or acct.get("status") != "ACTIVE":
        print(json.dumps({"error": f"No active {service} connection. Enable it via manage_config key='connections' first."}))
        sys.exit(1)

    # For Trello (OAuth1), fetch full account to get queryParams
    if service == "trello":
        acct_id = acct.get("id")
        if acct_id:
            full = _api_get(f"connected_accounts/{acct_id}")
            query_params = full.get("data", {}).get("queryParams", {}) or full.get("params", {}).get("queryParams", {})
            token = query_params.get("token") or _extract_token(acct)
            key = query_params.get("key")
            if not token:
                print(json.dumps({"error": "No token found for Trello"}))
                sys.exit(1)
            _write_token(svc["token_file"], token)
            if key and "key_file" in svc:
                _write_token(svc["key_file"], key)
            print(json.dumps({"status": "ok", "service": service, "token_file": svc["token_file"], "key_file": svc.get("key_file")}))
            return

    token = _extract_token(acct)
    if not token:
        print(json.dumps({"error": f"No access_token found for {service}. Check Composio connection."}))
        sys.exit(1)

    if token.endswith("..."):
        print(json.dumps({"error": "Token is masked. Disable secret masking in Composio project settings."}))
        sys.exit(1)

    _write_token(svc["token_file"], token)
    print(json.dumps({"status": "ok", "service": service, "token_file": svc["token_file"]}))


if __name__ == "__main__":
    main()

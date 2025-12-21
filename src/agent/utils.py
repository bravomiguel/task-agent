"""Utility functions for the agent."""

import json
import os
import requests

# =============================================================================
# COMPOSIO API HELPERS
# =============================================================================


def fetch_composio_gdrive_token() -> dict[str, str]:
    """Fetch Google Drive token from Composio API.

    Returns:
        Dict with RCLONE_CONFIG_* environment variables for Google Drive

    Raises:
        RuntimeError: If API call fails or token not found
    """
    composio_api_key = os.environ.get("COMPOSIO_API_KEY")
    if not composio_api_key:
        raise RuntimeError("COMPOSIO_API_KEY not found in environment")

    # Hardcoded connected account ID for now (will be dynamic later)
    connected_account_id = "ca_VHLP3Y6uKgAZ"

    url = f"https://backend.composio.dev/api/v3/connected_accounts/{connected_account_id}"
    headers = {"X-API-Key": composio_api_key}

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Extract tokens from response
        access_token = data.get("data", {}).get("access_token")
        refresh_token = data.get("data", {}).get("refresh_token")

        if not access_token or not refresh_token:
            raise RuntimeError("access_token or refresh_token not found in Composio response")

        # Build rclone token JSON
        token_json = json.dumps({
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer"
        })

        # Return environment variables for rclone
        return {
            "RCLONE_CONFIG_GDRIVE_TYPE": "drive",
            "RCLONE_CONFIG_GDRIVE_SCOPE": "drive",
            "RCLONE_CONFIG_GDRIVE_TOKEN": token_json,
        }

    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to fetch Composio token: {e}")

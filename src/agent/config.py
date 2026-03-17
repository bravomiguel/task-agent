"""config.py — User config schema, load/patch, cron reconciliation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from zoneinfo import available_timezones

import modal
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TIME_RE_END = re.compile(r"^(([01]\d|2[0-3]):([0-5]\d)|24:00)$")
_INTERVAL_RE = re.compile(r"^(\d+)\s*([smhd])$")


def _parse_every_to_cron(interval_str: str) -> str:
    """Convert interval string (e.g. '5m', '2h', '1d') to cron expression."""
    m = _INTERVAL_RE.fullmatch(interval_str.strip().lower())
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
# Pydantic models
# ---------------------------------------------------------------------------


DEFAULT_TIMEZONE = "UTC"
DEFAULT_ACTIVE_HOURS_START = "09:00"
DEFAULT_ACTIVE_HOURS_END = "21:00"
DEFAULT_HEARTBEAT_EVERY = "30m"


class ActiveHours(BaseModel):
    start: str = DEFAULT_ACTIVE_HOURS_START  # "HH:MM" 24h
    end: str = DEFAULT_ACTIVE_HOURS_END  # "HH:MM" or "24:00"

    @field_validator("start")
    @classmethod
    def validate_start(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError(f'Invalid start time "{v}": use HH:MM 24h format (00:00–23:59)')
        return v

    @field_validator("end")
    @classmethod
    def validate_end(cls, v: str) -> str:
        if not _TIME_RE_END.match(v):
            raise ValueError(f'Invalid end time "{v}": use HH:MM 24h format (00:00–24:00)')
        return v


class HeartbeatConfig(BaseModel):
    every: str = DEFAULT_HEARTBEAT_EVERY  # interval: "30m", "1h", "off"
    active_hours: ActiveHours = ActiveHours()

    @field_validator("every")
    @classmethod
    def validate_every(cls, v: str) -> str:
        if v.strip().lower() == "off":
            return "off"
        _parse_every_to_cron(v)  # raises on invalid
        return v


# TODO: Migrate UserProfile and HeartbeatConfig to Supabase users table.
# manage_config interface stays the same — only the backing store changes.

class UserProfile(BaseModel):
    """User profile — expandable over time."""
    timezone: str = DEFAULT_TIMEZONE  # IANA timezone

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError(f'Unknown timezone "{v}"')
        return v


class ActionGatingServices(BaseModel):
    """Per-service action gating toggles. True = require user approval for write/destructive actions."""
    google: bool = True
    github: bool = True
    notion: bool = True
    trello: bool = True
    slack: bool = True
    teams: bool = True
    microsoft: bool = True
    browser: bool = True


class ActionGatingConfig(BaseModel):
    """Action gating — require user approval for write/destructive actions on external services."""
    enabled: bool = True
    services: ActionGatingServices = ActionGatingServices()


class UserConfig(BaseModel):
    """Config stored in config.json. Only user profile, heartbeat, and action gating.

    Skills, connections, channels, and chat_surfaces are live from external
    sources (volume, Composio, vault) — not stored here.
    """
    user: UserProfile = UserProfile()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    action_gating: ActionGatingConfig = ActionGatingConfig()


# ---------------------------------------------------------------------------
# Load / patch / write
# ---------------------------------------------------------------------------

CONFIG_PATH = "/mnt/config.json"


def load_config(sandbox_id: str) -> UserConfig:
    """Read config from the Modal volume. Returns defaults if missing/invalid."""
    sandbox = modal.Sandbox.from_id(sandbox_id)
    try:
        process = sandbox.exec("cat", CONFIG_PATH, timeout=5)
        process.wait()
        raw = process.stdout.read().strip()
        if raw:
            data = json.loads(raw)
            return UserConfig.model_validate(data)
    except Exception as e:
        logger.warning("[Config] config file missing or invalid, using defaults: %s", e)

    return UserConfig()


def _deep_merge(base: dict, patch: dict) -> dict:
    """RFC 7386-style merge patch: recursively merge dicts, null deletes keys."""
    result = dict(base)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def patch_config(sandbox_id: str, patch: dict) -> UserConfig:
    """Merge patch into current config, validate, write back, return new config."""
    # Read current
    current = load_config(sandbox_id)
    current_data = current.model_dump(exclude_none=True)

    # Merge
    merged = _deep_merge(current_data, patch)

    # Validate
    new_config = UserConfig.model_validate(merged)

    # Write back
    _write_config(sandbox_id, new_config)
    return new_config


def _write_config(sandbox_id: str, config: UserConfig) -> None:
    """Write config JSON to the volume."""
    sandbox = modal.Sandbox.from_id(sandbox_id)
    data = json.dumps(config.model_dump(exclude_none=True), indent=2)
    process = sandbox.exec(
        "bash", "-c", f"cat > {CONFIG_PATH} << 'CONFIGEOF'\n{data}\nCONFIGEOF",
        timeout=5,
    )
    process.wait()
    # Sync volume so dashboard can see the change
    sandbox.exec("bash", "-c", "sync", timeout=5).wait()


# ---------------------------------------------------------------------------
# Heartbeat cron reconciliation
# ---------------------------------------------------------------------------


HEARTBEAT_INPUT_MESSAGE = (
    "Read HEARTBEAT.md from your project context and execute any tasks listed. "
    "Do not infer or repeat old tasks from prior sessions."
)


def _build_heartbeat_body(config: UserConfig, job_id: int) -> dict:
    """Build the POST body for the heartbeat cron job, including active hours."""
    body: dict[str, Any] = {
        "job_name": "heartbeat",
        "input_message": HEARTBEAT_INPUT_MESSAGE,
        "session_type": "main",
        "once": False,
        "job_id": job_id,
        "schedule_type": "cron",
        "timezone": config.user.timezone,
        "active_hours_start": config.heartbeat.active_hours.start,
        "active_hours_end": config.heartbeat.active_hours.end,
    }
    return body


def reconcile_heartbeat_cron(config: UserConfig) -> None:
    """Sync heartbeat cron schedule and active hours to match config.

    Compares desired interval against current heartbeat cron job in Supabase.
    Updates the cron schedule if they differ. Also updates the job body with
    current timezone and active hours so the cron-launcher can gate firings.
    Deactivates if "off".
    """
    disabled = config.heartbeat.every == "off"

    try:
        from agent.tools import _get_supabase

        sb = _get_supabase()
        result = sb.rpc("list_agent_crons").execute()
        jobs = result.data or []

        heartbeat_job = None
        for job in jobs:
            if "heartbeat" in (job.get("jobname") or "").lower():
                heartbeat_job = job
                break

        if not heartbeat_job:
            return  # No heartbeat job exists yet

        job_id = heartbeat_job.get("jobid")
        is_active = heartbeat_job.get("active", True)

        if disabled:
            if is_active:
                sb.rpc("update_agent_cron", {
                    "job_id": job_id,
                    "new_active": False,
                }).execute()
                logger.info("[Config] deactivated heartbeat cron")
            return

        # Re-activate if currently inactive
        desired_cron = _parse_every_to_cron(config.heartbeat.every)
        current_schedule = heartbeat_job.get("schedule", "")
        schedule_changed = current_schedule != desired_cron or not is_active

        if schedule_changed:
            params: dict[str, Any] = {"job_id": job_id, "new_schedule": desired_cron}
            if not is_active:
                params["new_active"] = True
            sb.rpc("update_agent_cron", params).execute()
            logger.info(
                "[Config] reconciled heartbeat cron: %s → %s%s",
                current_schedule,
                desired_cron,
                " (reactivated)" if not is_active else "",
            )

        # Always update the job body with current timezone + active hours
        new_body = _build_heartbeat_body(config, job_id)
        sb.rpc("update_agent_cron_body", {
            "job_id": job_id,
            "new_body": json.dumps(new_body),
        }).execute()
        logger.info(
            "[Config] synced heartbeat body: tz=%s hours=%s-%s",
            config.user.timezone,
            config.heartbeat.active_hours.start,
            config.heartbeat.active_hours.end,
        )

    except Exception as e:
        logger.warning("[Config] failed to reconcile heartbeat cron: %s", e)


def apply_config_side_effects(
    config: UserConfig,
    sandbox_id: str | None = None,
    patch: dict | None = None,
) -> dict[str, Any] | None:
    """Apply all side-effects for a config change.

    Called by manage_config tool after patch.
    Config.json only holds user + heartbeat now.
    """
    reconcile_heartbeat_cron(config)
    if sandbox_id and patch and "user" in patch and "timezone" in patch.get("user", {}):
        _sync_timezone_to_user_md(sandbox_id, config.user.timezone)
    return None


# ---------------------------------------------------------------------------
# Skills registry — all known skills with descriptions
# ---------------------------------------------------------------------------

SKILLS_REGISTRY: dict[str, str] = {
    "browser": "Browser automation via agent-browser CLI. Use for web scraping, form filling, site interaction, checking dashboards, logging into sites, and any task requiring a web browser.",
    "cloud-storage": "Cloud storage file management for Dropbox and Box via rclone.",
    "docx": "Comprehensive document creation, editing, and analysis with support for tracked changes, comments, and formatting preservation.",
    "gemini": "Gemini CLI for one-shot Q&A, summaries, and generation.",
    "github": "GitHub operations via gh CLI: issues, PRs, CI runs, code review, API queries.",
    "google": "Google Workspace CLI for Gmail, Calendar, Drive, Contacts, Sheets, and Docs via gog.",
    "microsoft": "Microsoft 365 — Outlook mail, Calendar, OneDrive, To Do tasks, and Contacts via MS Graph API.",
    "notion": "Notion API for creating and managing pages, databases, and blocks.",
    "openai-image-gen": "Batch-generate images via OpenAI Images API.",
    "openai-whisper-api": "Transcribe audio via OpenAI Audio Transcriptions API (Whisper).",
    "pdf": "Comprehensive PDF manipulation toolkit for extracting text, creating PDFs, merging/splitting documents, and handling forms.",
    "pptx": "Presentation creation, editing, and analysis.",
    "slack": "Send messages and interact with Slack workspaces.",
    "teams": "Send messages and interact with Microsoft Teams via the Graph API.",
    "trello": "Manage Trello boards, lists, and cards via the Trello REST API.",
    "weather": "Get current weather and forecasts via Open-Meteo. No API key needed.",
    "xlsx": "Comprehensive spreadsheet creation, editing, and analysis with support for formulas, formatting, and visualization.",
}

# All skills enabled by default on factory reset
CORE_SKILLS = set(SKILLS_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Skill volume sync (enable → fetch from GitHub, disable → delete)
# ---------------------------------------------------------------------------

SKILLS_REPO = "bravomiguel/task-agent"
SKILLS_REPO_DIR = "skills"
SKILLS_VOLUME_DIR = "/mnt/skills"
GITHUB_API_URL = "https://api.github.com"


def _read_skill_description(sandbox, skill_path: str) -> str:
    """Read the description from a skill's SKILL.md frontmatter."""
    import re

    try:
        process = sandbox.exec("head", "-20", f"{skill_path}/SKILL.md", timeout=5)
        process.wait()
        content = process.stdout.read()
        if not content:
            return ""
        fm = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if fm:
            for line in fm.group(1).splitlines():
                m = re.match(r"^description:\s*(.+)$", line.strip())
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return ""


def sync_skill_to_volume(
    sandbox_id: str, skill_name: str, enable: bool,
) -> dict[str, Any]:
    """Enable or disable a single skill on the volume.

    Enable: fetch skill folder from GitHub repo → write to volume.
    Disable: delete folder from volume.
    """
    import httpx

    sandbox = modal.Sandbox.from_id(sandbox_id)
    skill_path = f"{SKILLS_VOLUME_DIR}/{skill_name}"

    if not enable:
        try:
            sandbox.exec("bash", "-c", f"rm -rf {skill_path}", timeout=10).wait()
            sandbox.exec("bash", "-c", "sync", timeout=5).wait()
            logger.info("[Config] deleted skill %s from volume", skill_name)
            return {"status": "disabled", "skill": skill_name}
        except Exception as e:
            logger.warning("[Config] failed to delete skill %s: %s", skill_name, e)
            return {"status": "error", "error": str(e)}

    # Enable: fetch from GitHub and write to volume
    try:
        repo_path = f"{SKILLS_REPO_DIR}/{skill_name}"
        contents_url = f"{GITHUB_API_URL}/repos/{SKILLS_REPO}/contents/{repo_path}"
        resp = httpx.get(contents_url, timeout=15)
        if resp.status_code == 404:
            return {"status": "error", "error": f"Skill '{skill_name}' not found in repo."}
        resp.raise_for_status()
        items = resp.json()

        sandbox.exec("bash", "-c", f"mkdir -p {skill_path}", timeout=5).wait()
        _fetch_github_dir(sandbox, items, skill_path)
        sandbox.exec("bash", "-c", "sync", timeout=5).wait()
        logger.info("[Config] fetched skill %s from GitHub to %s", skill_name, skill_path)
        return {"status": "enabled", "skill": skill_name, "path": f"{skill_path}/SKILL.md"}

    except Exception as e:
        logger.warning("[Config] failed to fetch skill %s: %s", skill_name, e)
        return {"status": "error", "error": str(e)}


def _fetch_github_dir(sandbox, items: list[dict], dest_dir: str) -> None:
    """Recursively fetch files from a GitHub directory listing into the sandbox."""
    import httpx

    for item in items:
        name = item["name"]
        dest_path = f"{dest_dir}/{name}"

        if item["type"] == "file":
            # Download raw file content
            resp = httpx.get(item["download_url"], timeout=15)
            resp.raise_for_status()
            content = resp.text
            # Write to sandbox
            escaped = content.replace("'", "'\\''")
            sandbox.exec(
                "bash", "-c",
                f"printf '%s' '{escaped}' > {dest_path}",
                timeout=10,
            ).wait()

        elif item["type"] == "dir":
            # Recurse into subdirectory
            sandbox.exec(
                "bash", "-c", f"mkdir -p {dest_path}",
                timeout=5,
            ).wait()
            resp = httpx.get(item["url"], timeout=15)
            resp.raise_for_status()
            _fetch_github_dir(sandbox, resp.json(), dest_path)


USER_MD_PATH = "/mnt/prompts/USER.md"
_TZ_LINE_RE = re.compile(r"^(- \*\*Timezone\*\*:).*$", re.MULTILINE)


def _sync_timezone_to_user_md(sandbox_id: str, timezone: str) -> None:
    """Update the Timezone line in USER.md to match config.user.timezone."""
    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        process = sandbox.exec("cat", USER_MD_PATH, timeout=5)
        process.wait()
        content = process.stdout.read()
        if not content:
            return

        new_line = f"- **Timezone**: {timezone} <!-- Managed by config. Update via manage_config, not by editing this file. -->"
        new_content, count = _TZ_LINE_RE.subn(new_line, content)
        if count == 0 or new_content == content:
            return

        escaped = new_content.replace("'", "'\\''")
        sandbox.exec(
            "bash", "-c", f"printf '%s' '{escaped}' > {USER_MD_PATH}",
            timeout=5,
        ).wait()
        sandbox.exec("bash", "-c", "sync", timeout=5).wait()
        logger.info("[Config] synced timezone %s to USER.md", timezone)

    except Exception as e:
        logger.warning("[Config] failed to sync timezone to USER.md: %s", e)

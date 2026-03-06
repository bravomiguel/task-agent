"""config.py — User config schema, load/patch, active hours check, cron reconciliation."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

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


class UserConfig(BaseModel):
    timezone: str = DEFAULT_TIMEZONE  # IANA timezone — global user timezone
    heartbeat: HeartbeatConfig = HeartbeatConfig()

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError(f'Unknown timezone "{v}"')
        return v


# ---------------------------------------------------------------------------
# Load / patch / write
# ---------------------------------------------------------------------------

CONFIG_PATH = "/default-user/config.json"


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
# Active hours check
# ---------------------------------------------------------------------------


def is_within_active_hours(config: UserConfig, now: datetime | None = None) -> bool:
    """Check if current time is within configured active hours.

    Supports midnight wrap-around (e.g. 22:00–06:00).
    Uses config.timezone (top-level) for timezone resolution.
    """
    ah = config.heartbeat.active_hours

    # Parse to minutes since midnight
    start_min = _time_to_minutes(ah.start)
    end_min = _time_to_minutes(ah.end)

    if start_min == end_min:
        return False  # e.g. 09:00–09:00 = always inactive

    # Resolve current time in user's timezone
    tz = ZoneInfo(config.timezone)
    if now is None:
        now = datetime.now(tz=tz)
    else:
        now = now.astimezone(tz)

    current_min = now.hour * 60 + now.minute

    # Normal range (no wrap-around)
    if end_min > start_min:
        return start_min <= current_min < end_min

    # Wrap-around (e.g. 22:00–06:00)
    return current_min >= start_min or current_min < end_min


def _time_to_minutes(time_str: str) -> int:
    """Convert "HH:MM" to minutes since midnight. "24:00" → 1440."""
    parts = time_str.split(":")
    return int(parts[0]) * 60 + int(parts[1])


# ---------------------------------------------------------------------------
# Heartbeat cron reconciliation
# ---------------------------------------------------------------------------


def reconcile_heartbeat_cron(config: UserConfig) -> None:
    """Sync heartbeat cron schedule to match config.heartbeat.every.

    Compares desired interval against current heartbeat cron job in Supabase.
    Updates the cron schedule if they differ. Deactivates if "off".
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
            return  # No heartbeat job exists yet — ConfigMiddleware will create it

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
        needs_update = current_schedule != desired_cron or not is_active

        if needs_update:
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

    except Exception as e:
        logger.warning("[Config] failed to reconcile heartbeat cron: %s", e)


def apply_config_side_effects(config: UserConfig, sandbox_id: str | None = None) -> None:
    """Apply all side-effects for a config change.

    Called by manage_config tool after patch.
    """
    reconcile_heartbeat_cron(config)
    if sandbox_id:
        _sync_timezone_to_user_md(sandbox_id, config.timezone)


USER_MD_PATH = "/default-user/prompts/USER.md"
_TZ_LINE_RE = re.compile(r"^(- \*\*Timezone\*\*:).*$", re.MULTILINE)


def _sync_timezone_to_user_md(sandbox_id: str, timezone: str) -> None:
    """Update the Timezone line in USER.md to match config.timezone."""
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

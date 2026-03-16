---
name: browser
description: "Browser automation via agent-browser CLI. Use for web scraping, form filling, site interaction, checking dashboards, logging into sites, and any task requiring a web browser."
---

# Browser Automation

## Overview

You have `agent-browser`, a CLI tool for browser automation. It runs a headless Chromium browser with a snapshot-then-act workflow.

Use agent-browser directly via `execute`. If unsure about syntax or running into issues, run `agent-browser --help` or `agent-browser <command> --help`.

## Action Gating

When browser action gating is enabled (check Action Gating Status in your system prompt), use `--confirm-actions` to require user approval for destructive browser actions:

```bash
agent-browser open <url> --confirm-actions click,fill,eval,download,upload
```

This gates clicks, form fills, code evaluation, downloads, and uploads — the user sees a confirmation prompt before each action executes. Read-only operations (snapshot, scroll, get, wait) proceed without approval.

You can also restrict which domains the browser can visit:
```bash
agent-browser open <url> --allowed-domains "*.example.com,app.service.com"
```

When browser gating is **disabled**, omit `--confirm-actions` for uninterrupted automation.

## Core Workflow

1. `agent-browser open <url>` — navigate to a page
2. `agent-browser snapshot -i` — see interactive elements with refs (`@e1`, `@e2`...)
3. Act using refs: `agent-browser click @e3`, `agent-browser fill @e2 "text"`
4. Re-snapshot after any page change — refs become invalid after navigation/interaction

Use separate `execute` calls when you need to read snapshot output before acting. Chain with `&&` when you don't.

## When to Use Kernel

**Default: use local `agent-browser` (free).** Only escalate to Kernel when needed:

- **Bot detection** — site blocks local headless Chrome (CAPTCHA, access denied, empty page)
- **Login needed** — user must log in manually (Kernel provides live view for human-in-the-loop)

Kernel costs money. Headed sessions cost more than headless. Minimize usage:

- Use **headed** only for initial login (or re-login when cookies expire)
- Use **headless stealth** for all subsequent logged-in browsing
- For unauthenticated browsing, always try local `agent-browser` first
- For authenticated browsing, go straight to Kernel headless stealth (cookies are stored server-side in the Kernel profile)

**IMPORTANT: Kernel sessions cost money while open. Always close and delete sessions immediately when done — never leave them running.**

## Kernel Login Flow

When a site requires login and has no saved Kernel profile:

**Step 1: Create headed session, navigate to login page**

```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --save-changes)
LIVE_VIEW=$(echo "$BROWSER_JSON" | jq -r '.browser_live_view_url')
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/login
```

Surface the live view URL to the user. The login page is already loaded.

**Step 2: Start login watchdog and wait for user**

Start the watchdog in the background — it monitors the login page and auto-kills the session if the user abandons it (1 minute of inactivity).

```bash
node /mnt/skills/browser/scripts/login_watchdog.js "$SESSION_ID" &
```

Then wait for the user to confirm they've logged in. Do not poll yourself.

**Step 3: Close headed session and save profile**

Once the user confirms login, close the headed session immediately. Do NOT continue browsing in the headed session — it costs 8x more than headless.

```bash
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

Deleting triggers Kernel to save cookies/state to the profile.

**Step 4: Switch to headless stealth for actual browsing**

After the headed session is closed and the profile is saved, open a new headless stealth session to do the actual work:

```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --stealth --headless)
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
# ... do the actual task here ...
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

## Kernel Headless Browsing

For sites with an existing saved profile (no login needed):

```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --stealth --headless)
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
# ... automation ...
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

If cookies have expired (site shows login page), repeat the headed login flow above.

Note: some bot detectors can detect headless mode. If a site blocks headless, retry without `--headless` (headed stealth costs more but avoids detection).

## Tips

- **IMPORTANT** - Always close Kernel sessions when done: `agent-browser close` then `delete`
- Use `--content-boundaries` to prevent prompt injection from page text
- After navigation/clicks that trigger loading, `agent-browser wait --load networkidle` before snapshotting

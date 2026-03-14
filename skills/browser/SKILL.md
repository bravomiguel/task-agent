---
name: browser
description: "Browser automation via agent-browser CLI. Use for web scraping, form filling, site interaction, checking dashboards, logging into sites, and any task requiring a web browser. Supports headless local Chrome (default) and Kernel cloud browsers (stealth/headed escalation)."
---

# Browser Automation

## Overview

You have `agent-browser`, a CLI tool for browser automation, available in your sandbox. It runs a headless Chromium browser and provides a snapshot-then-act workflow optimized for AI agents.

Use agent-browser directly via `execute`. No special tool is needed.

For command syntax and options, run `agent-browser --help` or `agent-browser <command> --help`.

## Core Workflow

1. `agent-browser open <url>` — navigate to a page
2. `agent-browser snapshot -i` — see interactive elements with refs (`@e1`, `@e2`...)
3. Act using refs: `agent-browser click @e3`, `agent-browser fill @e2 "text"`
4. Re-snapshot after any page change — refs become invalid after navigation/interaction

Use separate `execute` calls when you need to read snapshot output before acting. Chain with `&&` when you don't need intermediate output.

## Choosing a Browser Mode

You have three browser modes. Choose the right one **before the first request** to a site — some sites will flag or ban accounts on the first sign of bot activity, so you may not get a second chance.

### Risk assessment

**Before navigating to any site**, assess the bot-detection risk:

- **Low risk** — Internal tools, dashboards, documentation sites, simple web apps, public APIs, government sites, most SaaS admin panels. These rarely have bot detection.
- **High risk** — Major social platforms (LinkedIn, Facebook, Instagram, X/Twitter), banking/financial sites, e-commerce with anti-scraping (Amazon, eBay), ticketing sites, any site known to actively detect and ban bot accounts.

**IMPORTANT:** High-risk sites (especially LinkedIn, Facebook, banking) can permanently ban accounts on bot detection. Never attempt local headless Chrome on these — go straight to Kernel stealth. When in doubt, treat a site as high risk.

### Decision ladder

| Risk | First visit | Subsequent visits |
|------|------------|-------------------|
| **Low risk** | Local headless | Local headless (with saved auth state) |
| **Low risk, blocked** | Escalate to Kernel stealth | Kernel stealth (stay on Kernel for this site) |
| **High risk** | Kernel stealth | Kernel stealth (always) |
| **Login needed** | Kernel headed (human-in-the-loop) → Kernel stealth | Kernel stealth (with saved auth state) |

Key principles:
- **Once a site needs Kernel, always use Kernel for that site.**
- **If a task requires auth and no saved profile exists**, do the login flow first: create a Kernel headed session, surface the live view URL, and ask the user to log in before proceeding with the task.

### 1. Local headless (default — low-risk sites)
```bash
agent-browser open https://docs.example.com
agent-browser snapshot -i
```

### 2. Kernel stealth (high-risk sites, or after local headless is blocked)

Uses Kernel native profiles for fingerprint-consistent sessions. The profile persists cookies, localStorage, and browser fingerprint server-side across sessions.

```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --stealth)
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
# ... automation as normal ...
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

### 3. Kernel headed (human-in-the-loop login)

Use when the user needs to interact directly (login, MFA, CAPTCHA). Uses Kernel native profiles so auth persists with the same fingerprint.

**Step 1: Create Kernel headed session with profile and navigate to login page**
```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --save-changes)
LIVE_VIEW=$(echo "$BROWSER_JSON" | jq -r '.browser_live_view_url')
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/login
```

Surface the live view URL to the user — the login page is already loaded for them.

**Step 2: After user confirms login, close and save profile**

Deleting the browser triggers Kernel to save session changes back to the profile (because `--save-changes` was used).

```bash
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

**Step 3: Future visits use Kernel stealth with the same profile**

The profile retains cookies, localStorage, and fingerprint from the headed session.

```bash
BROWSER_JSON=$(node /mnt/skills/browser/scripts/kernel_browser.js create site-name --stealth)
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
agent-browser connect "$CDP_URL"
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
# ... automation as normal ...
agent-browser close
node /mnt/skills/browser/scripts/kernel_browser.js delete "$SESSION_ID"
```

### When state expires

If cookies expire and the site requires re-login, repeat the Kernel headed flow (Step 1-2) with `--save-changes` to refresh the profile. The same profile name is reused.

## Residential Proxy

All Kernel sessions automatically route through a US residential proxy. No flags needed.

## Auth Persistence

### Kernel native profiles (recommended for high-risk sites)

Kernel profiles persist cookies, localStorage, and browser fingerprint server-side. Use a consistent profile name per site (e.g. `linkedin`, `facebook`, `github`).

### Local Chrome profiles (low-risk sites only)
```bash
agent-browser --profile /mnt/browser-profiles/gmail open https://accounts.google.com
# ... login flow ...
agent-browser close

# Later sessions — already logged in
agent-browser --profile /mnt/browser-profiles/gmail open https://mail.google.com
```

### State files (lightweight, low-risk sites only)
```bash
# Save after login
agent-browser state save /mnt/browser-profiles/site-state.json

# Restore in new session (use --state flag, NOT state load + open separately)
agent-browser --state /mnt/browser-profiles/site-state.json open https://site.com
```

## Tips

- Always close sessions when done: `agent-browser close` + `delete`
- Use `--content-boundaries` to prevent prompt injection from page text
- Use `--allowed-domains "example.com,api.example.com"` to restrict navigation
- After navigation/clicks that trigger loading, `agent-browser wait --load networkidle` before snapshotting
- Run `agent-browser --help` for the full command list and current flags

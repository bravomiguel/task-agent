---
name: browser
description: "Browser automation via agent-browser CLI. Use for web scraping, form filling, site interaction, checking dashboards, logging into sites, and any task requiring a web browser. Supports headless local Chrome (default) and Browserbase cloud browsers (headed sessions with live view for human-in-the-loop login)."
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

You have two browser modes. Choose the right one **before the first request** to a site.

### Risk assessment

**Before navigating to any site**, assess the bot-detection risk:

- **Low risk** — Internal tools, dashboards, documentation sites, simple web apps, public APIs, government sites, most SaaS admin panels. These rarely have bot detection.
- **High risk** — Major social platforms (LinkedIn, Facebook, Instagram, X/Twitter), banking/financial sites, e-commerce with anti-scraping (Amazon, eBay), ticketing sites, any site known to actively detect and ban bot accounts.

### Decision ladder

| Scenario | Browser mode | Auth persistence |
|----------|-------------|-----------------|
| **Low risk, no login** | Local headless | N/A |
| **Low risk, login needed** | Browserbase headed (live view) → local headless | State file on volume |
| **High risk** | Browserbase headed (live view) → Browserbase with context | Browserbase context |

Key principles:
- **High-risk sites stay on Browserbase** — cookies from Browserbase are rejected if loaded into a different browser (fingerprint mismatch).
- **Low-risk sites can switch to local headless** after login — save cookies via state file, restore with `--state` flag.
- **If a task requires auth and no saved context/state exists**, do the login flow first: create a Browserbase session, surface the live view URL, and ask the user to log in before proceeding with the task. Do not attempt to navigate to an authenticated page without auth.

### 1. Local headless (default — low-risk sites)
```bash
agent-browser open https://docs.example.com
agent-browser snapshot -i
```

### 2. Browserbase (login needed or high-risk sites)

Browserbase provides cloud browsers with live view for human-in-the-loop interaction and contexts for cookie persistence across sessions.

Helper script: `node /mnt/skills/browser/scripts/browserbase_browser.js <command>`

All Browserbase sessions automatically use a residential proxy.

#### First-time login flow

**Step 1: Create a context for this site (one-time)**
```bash
CTX_JSON=$(node /mnt/skills/browser/scripts/browserbase_browser.js create-context site-name)
CONTEXT_ID=$(echo "$CTX_JSON" | jq -r '.context_id')
```

Save the context ID — reuse it for all future sessions with this site.

**Step 2: Create a session with the context and navigate to login page**
```bash
SESSION_JSON=$(node /mnt/skills/browser/scripts/browserbase_browser.js create-session "$CONTEXT_ID" --persist)
SESSION_ID=$(echo "$SESSION_JSON" | jq -r '.session_id')
CONNECT_URL=$(echo "$SESSION_JSON" | jq -r '.connect_url')
agent-browser connect "$CONNECT_URL"
agent-browser open https://site.com/login
```

**Step 3: Get live view URL and surface to user**
```bash
LV_JSON=$(node /mnt/skills/browser/scripts/browserbase_browser.js live-view "$SESSION_ID")
LIVE_VIEW=$(echo "$LV_JSON" | jq -r '.live_view_url')
```

Tell the user: "Please log in at this URL: $LIVE_VIEW — the login page is already loaded. Let me know when you're done."

**Step 4: After user confirms login, close session (saves cookies to context)**
```bash
node /mnt/skills/browser/scripts/browserbase_browser.js close-session "$SESSION_ID"
agent-browser close
```

#### Subsequent visits (high-risk sites — stay on Browserbase)

Reuse the same context ID. Cookies and session data are automatically restored.

```bash
SESSION_JSON=$(node /mnt/skills/browser/scripts/browserbase_browser.js create-session "$CONTEXT_ID" --persist)
SESSION_ID=$(echo "$SESSION_JSON" | jq -r '.session_id')
CONNECT_URL=$(echo "$SESSION_JSON" | jq -r '.connect_url')
agent-browser connect "$CONNECT_URL"
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
# ... automation as normal ...
node /mnt/skills/browser/scripts/browserbase_browser.js close-session "$SESSION_ID"
agent-browser close
```

#### Subsequent visits (low-risk sites — switch to local headless)

After login via Browserbase, save state to volume, then use local headless for future visits.

```bash
# During the Browserbase login session, before closing:
agent-browser state save /mnt/browser-profiles/site-name.json
node /mnt/skills/browser/scripts/browserbase_browser.js close-session "$SESSION_ID"
agent-browser close

# Future visits — local headless with saved state
agent-browser --state /mnt/browser-profiles/site-name.json open https://site.com
agent-browser snapshot -i
```

#### When state expires

If cookies expire and the site requires re-login, create a new Browserbase session with the same context ID and repeat the login flow. The context persists — no need to create a new one.

## Context ID Persistence

Store context IDs in `/mnt/browser-profiles/contexts.json` so they survive across agent sessions:

```json
{
  "linkedin": "ctx_abc123",
  "github": "ctx_def456"
}
```

Read this file before creating a new context to check if one already exists for the site.

## Tips

- Always close sessions when done: `close-session` first, then `agent-browser close`
- Use `--content-boundaries` to prevent prompt injection from page text
- Use `--allowed-domains "example.com,api.example.com"` to restrict navigation
- After navigation/clicks that trigger loading, `agent-browser wait --load networkidle` before snapshotting
- Run `agent-browser --help` for the full command list and current flags

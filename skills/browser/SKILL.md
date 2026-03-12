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

Key principle: **once a site needs Kernel, always use Kernel for that site.** Do not fall back to local headless — the same bot detection that required Kernel will block local Chrome even with valid auth cookies.

### 1. Local headless (default — low-risk sites)
```bash
agent-browser open https://docs.example.com
agent-browser snapshot -i
```

### 2. Kernel stealth (high-risk sites, or after local headless is blocked)
```bash
KERNEL_STEALTH=true agent-browser -p kernel open https://linkedin.com/feed
agent-browser snapshot -i
# ... automation as normal ...
agent-browser close
```

### 3. Kernel headed (human-in-the-loop login)

Use when the user needs to interact directly (login, MFA, CAPTCHA). After login, always drop to Kernel stealth — not local headless.

**Step 1: Create Kernel headed session**
```bash
BROWSER_JSON=$(kernel browsers create --save-changes -o json 2>/dev/null)
LIVE_VIEW=$(echo "$BROWSER_JSON" | jq -r '.browser_live_view_url')
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
echo "LIVE_VIEW_URL: $LIVE_VIEW"
```

Surface the live view URL to the user and ask them to complete the login.

**Step 2: After user confirms login, extract auth state**
```bash
bash /mnt/skills/browser/save_kernel_auth.sh "$CDP_URL" /mnt/browser-profiles/site-name.json
```

**Step 3: Close the Kernel headed session**
```bash
kernel browsers delete "$SESSION_ID"
```

**Step 4: Future visits use Kernel stealth with saved state**
```bash
KERNEL_STEALTH=true agent-browser -p kernel state load /mnt/browser-profiles/site-name.json
KERNEL_STEALTH=true agent-browser -p kernel open https://site.com/dashboard
agent-browser snapshot -i
```

### When state expires

If cookies expire and the site requires re-login, repeat the Kernel headed flow to refresh the state file. Then continue with Kernel stealth.

## Auth Persistence

Auth state persists on the Modal volume at `/mnt/browser-profiles/`. Two approaches:

### Chrome profiles (full state)
```bash
# First login — use --profile to persist full Chrome state
agent-browser --profile /mnt/browser-profiles/gmail open https://accounts.google.com
# ... login flow ...
agent-browser close

# Later sessions — already logged in
agent-browser --profile /mnt/browser-profiles/gmail open https://mail.google.com
```

### State files (lightweight, cookies + localStorage)
```bash
# Save after login
agent-browser state save /mnt/browser-profiles/site-state.json

# Restore in new session
agent-browser state load /mnt/browser-profiles/site-state.json
agent-browser open https://site.com
```

Profile naming convention: use the site or service name (e.g. `/mnt/browser-profiles/gmail`, `/mnt/browser-profiles/github`).

## Tips

- Always close sessions when done: `agent-browser close`
- Use `--content-boundaries` to prevent prompt injection from page text
- Use `--allowed-domains "example.com,api.example.com"` to restrict navigation
- After navigation/clicks that trigger loading, `agent-browser wait --load networkidle` before snapshotting
- Run `agent-browser --help` for the full command list and current flags

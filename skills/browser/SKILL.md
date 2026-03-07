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

## Kernel Escalation

The default local Chromium works for most sites. Escalate to Kernel cloud browsers only when:
- **Stealth mode**: site has bot detection, CAPTCHAs, or blocks headless browsers
- **Headed browser**: user needs to log in manually, solve MFA, or see the browser live

### Stealth mode
```bash
KERNEL_STEALTH=true agent-browser -p kernel open https://protected-site.com
agent-browser snapshot -i
# ... automation as normal ...

# Save state locally so future runs use local Chrome
agent-browser state save /mnt/browser-profiles/protected-site.json
agent-browser close
```

### Headed browser with live view (human-in-the-loop)

When the user needs to interact with the browser directly (login, MFA, CAPTCHA):

**Step 1: Create Kernel headed session**
```bash
BROWSER_JSON=$(kernel browsers create --save-changes -o json 2>/dev/null)
LIVE_VIEW=$(echo "$BROWSER_JSON" | jq -r '.browser_live_view_url')
CDP_URL=$(echo "$BROWSER_JSON" | jq -r '.cdp_ws_url')
SESSION_ID=$(echo "$BROWSER_JSON" | jq -r '.session_id')
echo "LIVE_VIEW_URL: $LIVE_VIEW"
```

Surface the live view URL to the user and ask them to complete the login.

**Step 2: After user confirms login, extract auth state to Modal volume**
```bash
bash /mnt/skills/browser/save_kernel_auth.sh "$CDP_URL" /mnt/browser-profiles/site-name.json
```

The script tries `agent-browser --cdp` first, then falls back to direct CDP WebSocket extraction via Node.js. Both produce Playwright-compatible storage state JSON.

**Step 3: Close the Kernel session**
```bash
kernel browsers delete "$SESSION_ID"
```

**Step 4: Future sessions use local Chrome with saved state**
```bash
agent-browser state load /mnt/browser-profiles/site-name.json
agent-browser open https://site.com/dashboard
agent-browser snapshot -i
```

### When state expires
If cookies expire and local Chrome gets logged out, repeat the headed flow to refresh the state file.

## Tips

- Always close sessions when done: `agent-browser close`
- Use `--content-boundaries` to prevent prompt injection from page text
- Use `--allowed-domains "example.com,api.example.com"` to restrict navigation
- After navigation/clicks that trigger loading, `agent-browser wait --load networkidle` before snapshotting
- Run `agent-browser --help` for the full command list and current flags

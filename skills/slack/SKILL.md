---
name: slack
description: "Send messages and interact with Slack workspaces. Supports bot and user identities."
---

# Slack Skill

## Sending Messages

Use the `send_message` tool:

```
send_message(platform="slack", recipient="C01234567", text="Hello!")
send_message(platform="slack", recipient="C01234567", text="Reply", thread_ts="1234567890.123456")
send_message(platform="slack", recipient="C01234567", text="As bot", as_identity="bot")
send_message(platform="slack", recipient="C01234567", text="As user", as_identity="user")
```

### as_identity parameter

- `"bot"` — sends as the Slack bot app (xoxb- token)
- `"user"` — sends as the connected user (Composio OAuth)
- `None` (default) — auto-selects bot if bot is enabled, otherwise user

## Common API Operations

For direct Slack API calls (beyond what `send_message` provides), get a token first:

```bash
# Bot token (if bot is connected)
manage_config action="get" key="chat_surfaces"  # check if Slack chat surface is set up

# User token (Composio OAuth)
fetch_auth service="slack"  # writes to /workspace/.auth/slack_token
export SLACK_TOKEN=$(cat /workspace/.auth/slack_token)
```

**Note:** For sending messages, prefer `send_message` tool — it handles token resolution automatically.

### List channels

```bash
curl -s "https://slack.com/api/conversations.list" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq '.channels[] | {name, id}'
```

### List users

```bash
curl -s "https://slack.com/api/users.list" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq '.members[] | {name: .real_name, id}'
```

### Get channel history

```bash
curl -s "https://slack.com/api/conversations.history?channel=C01234567&limit=20" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq '.messages[] | {user, text, ts}'
```

### Add a reaction

```bash
curl -s -X POST "https://slack.com/api/reactions.add" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "C01234567", "name": "thumbsup", "timestamp": "1234567890.123456"}'
```

## Notes

- Channel IDs start with `C`, user IDs with `U`, bot IDs with `B`
- Use channel IDs (not names) for reliable delivery
- Rate limits: Tier 1 (1/min), Tier 2 (20/min), Tier 3 (50/min), Tier 4 (100/min)
- `chat.postMessage` is Tier 2; `conversations.list` is Tier 2
- Max message length: 4000 characters (use blocks for longer content)

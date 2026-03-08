---
name: slack
description: "Send messages and interact with Slack workspaces via the Slack Web API. Use when: user asks to send a Slack message, list channels, or interact with Slack."
---

# Slack Skill

Send messages and interact with Slack workspaces via the Slack Web API.

## Authentication

Before using any Slack commands, set up auth for this session:

1. `manage_auth` tool with action `"list"` — check if Slack is connected
2. `manage_auth` tool with action `"connect"`, service `"slack"` — fetch token
3. Token is written to `/workspace/.auth/slack_token`

Set the token for API calls:

```bash
SLACK_TOKEN=$(cat /workspace/.auth/slack_token)
```

Run this before any Slack command. If you get a `token_expired` or `invalid_auth` error, re-run `manage_auth connect slack` to refresh.

## Sending Messages

Use the `send_message` tool for the simplest way to send a message:

```
send_message(platform="slack", recipient="C01234567", text="Hello!")
```

Or via curl for more control (threads, blocks, etc.):

```bash
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "C01234567", "text": "Hello!"}'
```

### Reply in a thread

```bash
curl -s -X POST "https://slack.com/api/chat.postMessage" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"channel": "C01234567", "text": "Thread reply", "thread_ts": "1234567890.123456"}'
```

## Common Operations

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

### Find a channel by name

```bash
curl -s "https://slack.com/api/conversations.list" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq '.channels[] | select(.name == "general") | .id'
```

### Get user info

```bash
curl -s "https://slack.com/api/users.info?user=U01234567" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq '.user | {name: .real_name, email: .profile.email}'
```

### Verify connection

```bash
curl -s "https://slack.com/api/auth.test" \
  -H "Authorization: Bearer $SLACK_TOKEN" | jq
```

## Notes

- Channel IDs start with `C`, user IDs with `U`, bot IDs with `B`
- Use channel IDs (not names) for reliable delivery
- Rate limits: Tier 1 (1/min), Tier 2 (20/min), Tier 3 (50/min), Tier 4 (100/min)
- `chat.postMessage` is Tier 2; `conversations.list` is Tier 2
- Max message length: 4000 characters (use blocks for longer content)

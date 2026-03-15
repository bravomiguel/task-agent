---
name: teams
description: "Send messages and interact with Microsoft Teams via the Graph API. Use when: user asks to send a Teams message, list teams/channels, or interact with Microsoft Teams."
---

# Microsoft Teams Skill

Send messages and interact with Microsoft Teams via the Microsoft Graph API.

## Authentication

Before using any Teams commands, set up auth for this session:

1. `manage_config` tool with action `"get"`, key `"connections"` — check connection status
2. `fetch_auth` tool with service `"teams"` — fetch token
3. Token is written to `/workspace/.auth/teams_token`

Set the token for API calls:

```bash
TEAMS_TOKEN=$(cat /workspace/.auth/teams_token)
```

Run this before any Teams command. If you get a 401 error, re-run `fetch_auth service="teams"` to refresh.

## Sending Messages

Use the `send_message` tool for the simplest way to send a message:

```
# 1:1 or group chat
send_message(platform="teams", recipient="{chatId}", text="Hello!")

# Channel message
send_message(platform="teams", recipient="team:{teamId}/channel:{channelId}", text="Hello!")
```

Or via curl:

### Send to a chat (1:1 or group)

```bash
curl -s -X POST "https://graph.microsoft.com/v1.0/chats/{chatId}/messages" \
  -H "Authorization: Bearer $TEAMS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": {"content": "Hello!"}}'
```

### Send to a channel

```bash
curl -s -X POST "https://graph.microsoft.com/v1.0/teams/{teamId}/channels/{channelId}/messages" \
  -H "Authorization: Bearer $TEAMS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": {"content": "Hello!"}}'
```

### Reply to a channel message

```bash
curl -s -X POST "https://graph.microsoft.com/v1.0/teams/{teamId}/channels/{channelId}/messages/{messageId}/replies" \
  -H "Authorization: Bearer $TEAMS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": {"content": "Reply text"}}'
```

## Common Operations

### Verify connection

```bash
curl -s "https://graph.microsoft.com/v1.0/me" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '{displayName, mail}'
```

### List joined teams

```bash
curl -s "https://graph.microsoft.com/v1.0/me/joinedTeams" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '.value[] | {displayName, id}'
```

### List channels in a team

```bash
curl -s "https://graph.microsoft.com/v1.0/teams/{teamId}/channels" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '.value[] | {displayName, id}'
```

### List recent chats

```bash
curl -s "https://graph.microsoft.com/v1.0/me/chats" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '.value[] | {topic, id, chatType}'
```

### Get chat messages

```bash
curl -s "https://graph.microsoft.com/v1.0/chats/{chatId}/messages?\$top=20" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '.value[] | {from: .from.user.displayName, content: .body.content, createdDateTime}'
```

### List team members

```bash
curl -s "https://graph.microsoft.com/v1.0/teams/{teamId}/members" \
  -H "Authorization: Bearer $TEAMS_TOKEN" | jq '.value[] | {displayName, email}'
```

## Notes

- Chat IDs and channel IDs are opaque strings from the Graph API
- Message body supports HTML content (e.g. `<b>bold</b>`, `<a href="...">link</a>`)
- Throttling: 2 requests/second for chat messages; higher for reads
- Delegated permissions require admin consent for some scopes
- Channel IDs look like `19:...@thread.tacv2`

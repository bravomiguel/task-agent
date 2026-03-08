---
name: microsoft
description: "Microsoft 365 — Outlook mail, Calendar, and Contacts via MS Graph API. Use when: user asks about Outlook, Microsoft email, calendar events, meetings, or contacts. NOT for: OneDrive files, Teams, SharePoint, or To Do tasks (separate integrations)."
---

# Microsoft 365 Skill

Access Microsoft 365 services — Email (Outlook), Calendar, and Contacts via the Microsoft Graph API.

## Authentication

Before using any Microsoft commands, set up auth for this session:

1. `manage_auth` tool with action `"list"` — check if Microsoft is connected
2. `manage_auth` tool with action `"connect"`, service `"microsoft"` — fetch credentials
3. Token written to `/workspace/.auth/microsoft_token`

If you get a 401 error, re-run `manage_auth connect microsoft` to refresh the token.

## CLI Usage

All commands use the `ms365_cli.py` script which calls the Microsoft Graph API directly.

```bash
CLI=/mnt/skills/microsoft/scripts/ms365_cli.py
```

### Current User

```bash
python3 $CLI user
```

### Email (Outlook)

```bash
# List recent emails
python3 $CLI mail list [--top N]

# List emails in a specific folder
python3 $CLI mail list --folder FOLDER_ID

# Read a specific email
python3 $CLI mail read MESSAGE_ID

# Send email
python3 $CLI mail send --to "recipient@example.com" --subject "Subject" --body "Message body"

# Send with CC
python3 $CLI mail send --to "a@example.com" --cc "b@example.com,c@example.com" --subject "Subject" --body "Body"

# Search emails
python3 $CLI mail search "project update" [--top N]
```

### Calendar

```bash
# List upcoming events
python3 $CLI calendar list [--top N]

# Create event
python3 $CLI calendar create --subject "Meeting" --start "2026-03-15T10:00:00" --end "2026-03-15T11:00:00" [--body "Description"] [--timezone "America/New_York"] [--location "Conference Room"] [--attendees "a@example.com,b@example.com"]
```

### Contacts

```bash
# List contacts
python3 $CLI contacts list [--top N]

# Search people
python3 $CLI contacts search "John"
```

## Examples

User: "Check my outlook email"
→ `python3 $CLI mail list --top 10`

User: "What meetings do I have?"
→ `python3 $CLI calendar list`

User: "Send an email to john@company.com about the project update"
→ `python3 $CLI mail send --to "john@company.com" --subject "Project Update" --body "..."`

User: "Search for emails from Sarah"
→ `python3 $CLI mail search "from:sarah"`

User: "Schedule a meeting with the team tomorrow at 2pm"
→ `python3 $CLI calendar create --subject "Team Meeting" --start "2026-03-09T14:00:00" --end "2026-03-09T15:00:00" --attendees "..."`

## Notes

- Use ISO 8601 datetime format for calendar events (e.g. `2026-03-15T10:00:00`)
- Default timezone is UTC — always specify `--timezone` for local times
- When sending email, confirm recipient and content with the user before sending
- Mail search uses Microsoft's KQL syntax: `from:name`, `subject:keyword`, `hasAttachments:true`
- Token expires after ~1 hour; re-run `manage_auth connect microsoft` to refresh
- This skill covers Mail, Calendar, and Contacts. OneDrive and To Do require separate service connections.

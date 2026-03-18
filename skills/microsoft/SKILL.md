---
name: microsoft
description: "Microsoft 365 — Outlook mail, Calendar, OneDrive, To Do tasks, and Contacts via MS Graph API. Use when: user asks about Outlook, Microsoft email, calendar events, meetings, OneDrive files, Microsoft tasks, or contacts."
---

# Microsoft 365 Skill

Access Microsoft 365 services — Email (Outlook), Calendar, OneDrive, To Do tasks, and Contacts via the Microsoft Graph API.

## Authentication

Token location: `/mnt/auth/microsoft_token`

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

### OneDrive Files

```bash
# List files in root
python3 $CLI files list

# List files in a folder
python3 $CLI files list --path "Documents/Reports"

# Search files
python3 $CLI files search "budget"

# Get file metadata
python3 $CLI files get FILE_ID

# Download a file
python3 $CLI files download FILE_ID [--output local_path]
```

### To Do Tasks

```bash
# List task lists
python3 $CLI tasks lists

# Get tasks from a list
python3 $CLI tasks get LIST_ID [--top N]

# Create task
python3 $CLI tasks create LIST_ID --title "Task title" [--due "2026-03-20"]

# Mark task complete
python3 $CLI tasks complete LIST_ID TASK_ID
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

User: "Show my OneDrive files"
→ `python3 $CLI files list`

User: "Find the budget spreadsheet"
→ `python3 $CLI files search "budget"`

User: "Add a task to review the budget"
→ List task lists first, then `python3 $CLI tasks create LIST_ID --title "Review the budget"`

User: "What's on my to do list?"
→ `python3 $CLI tasks lists`, then `python3 $CLI tasks get LIST_ID`

## Notes

- Use ISO 8601 datetime format for calendar events (e.g. `2026-03-15T10:00:00`)
- Default timezone is UTC — always specify `--timezone` for local times
- When sending email, confirm recipient and content with the user before sending
- Mail search uses Microsoft's KQL syntax: `from:name`, `subject:keyword`, `hasAttachments:true`
- For tasks, list available task lists first so user can choose the right one
- Token expires after ~1 hour; refresh credentials if you get a 401
- Requires Composio auth config with scopes: Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, Contacts.ReadWrite, Files.ReadWrite, Tasks.ReadWrite, User.Read

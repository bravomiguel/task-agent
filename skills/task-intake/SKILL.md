---
name: task-intake
description: Triage incoming webhook events (email, instant message, meeting) to decide task routing. Use when processing incoming messages/notifications to determine whether to: (1) create a new task thread, (2) add to an existing thread, or (3) ignore. Requires reading type-specific triage rules from /memories/{type}_triage.md and using langgraph-api skill for thread operations.
---

# Task Intake

Triage system for incoming webhook events.

## Workflow

1. **Read triage rules** from `/memories/{type}_triage.md` (e.g., `email_triage.md`)
2. **Search existing threads** using langgraph-api skill
3. **Decide**: new thread, existing thread, or ignore
4. **Execute** using langgraph-api skill

## Input Types

| Type | Providers | Memory file |
|------|-----------|-------------|
| `email` | Gmail, Outlook | `email_triage.md` |
| `instant_message` | Slack, Teams, Discord | `instant_message_triage.md` |
| `meeting` | Zoom, Google Calendar, Teams | `meeting_triage.md` |

## Input Format

Webhook events arrive wrapped in XML tags by type. The `source` attribute indicates the provider.

```xml
<email source="gmail">
Date: 2024-01-15T10:30:00Z
Subject: Q4 Budget Review Request
From: finance@company.com
Body: Please review the attached...
</email>
```

```xml
<instant_message source="slack">
Date: 2024-01-15T10:30:00Z
Channel: #engineering
User: @alice
Message: Can someone help debug the payment issue?
</instant_message>
```

```xml
<meeting source="zoom">
Start: 2024-01-15T14:00:00Z
Title: Q4 Planning Session
Organizer: ceo@company.com
Duration: 1 hour
Type: Recording Available
</meeting>
```

## Decision Framework

| Decision | When | Action |
|----------|------|--------|
| **Ignore** | Matches ignore pattern in triage rules | No action |
| **New Thread** | Novel task, no related open thread | Create thread + run |
| **Existing Thread** | Follow-up to open task | Add to existing thread |

## Steps

### 1. Read Triage Rules

Based on input type:

```
read_file("/memories/email_triage.md")
read_file("/memories/instant_message_triage.md")
read_file("/memories/meeting_triage.md")
```

### 2. Search Threads

Use langgraph-api skill - see [threads-search.md](/skills/langgraph-api/threads-search.md):

- Search for `is_done: false` threads
- Look for same sender, similar subject, related context

### 3. Execute Decision

Use langgraph-api skill:
- **New thread**: See [threads-create.md](/skills/langgraph-api/threads-create.md) then [runs-create.md](/skills/langgraph-api/runs-create.md)
- **Existing thread**: See [runs-create.md](/skills/langgraph-api/runs-create.md) with `multitask_strategy: "enqueue"`

### 4. Report

```
**Decision**: [New Thread | Existing Thread | Ignored]
**Reason**: Brief explanation
**Thread**: [thread_id if applicable]
```

## Thread Metadata

When creating threads:

```python
{
    "is_done": False,
    "title": "Brief title from content"
}
```

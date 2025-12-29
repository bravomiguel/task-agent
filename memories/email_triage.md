# Email Triage Rules

Rules for processing incoming emails (from any provider: Gmail, Outlook, etc.) and deciding whether to create tasks.

## Ignore

Skip these emails entirely - no action needed:

- **Newsletters**: from addresses containing `newsletter@`, `news@`, `digest@`, `weekly@`
- **Marketing**: from addresses containing `marketing@`, `promo@`, `offers@`
- **Auto-replies**: subject starts with "Out of Office", "Automatic reply", "Auto:"
- **No-reply**: from addresses containing `noreply@`, `no-reply@`, `donotreply@`
- **Notifications**: from addresses containing `notifications@`, `notify@`, `alerts@`
- **Unsubscribe-only**: emails that are purely promotional with unsubscribe links

## Priority Indicators

These signals suggest the email may be urgent or important:

- **Sender**: from known important contacts (CEO, direct reports, key clients)
- **Subject keywords**: URGENT, ASAP, EOD, "by end of day", "time sensitive", CRITICAL
- **CC list**: includes legal@, compliance@, finance@ (potential escalation)
- **Reply chains**: "Re: Re: Re:" suggests ongoing discussion needing attention
- **Direct questions**: emails ending with "?" or containing "can you", "could you", "please"

## Decision Framework

### Create New Task When:

1. Email contains a clear request or action item
2. Email requires research, analysis, or deliverable
3. Email is from a person (not automated system)
4. Response would take more than 2 minutes to complete
5. Email starts a new topic/thread not related to existing tasks

### Add to Existing Task When:

1. Email is a reply to an ongoing conversation
2. Email references a project or task already in progress
3. Subject line matches or relates to existing thread title
4. Same sender has recent open task on similar topic

### No Action When:

1. Email matches ignore patterns above
2. Email is purely informational (no action requested)
3. Email is a confirmation/receipt (order confirmations, calendar accepts)
4. Email is spam or clearly not relevant

## Example Decisions

### Create New Task
```
Subject: Q4 Budget Review Needed
From: cfo@company.com
Body: Can you review the attached Q4 budget proposal and provide feedback by Friday?

Decision: NEW TASK
Reason: Clear request with deadline from important sender
Title: "Q4 Budget Review Needed"
```

### Add to Existing Task
```
Subject: Re: Website redesign project
From: alice@company.com
Body: Here are the updated mockups you requested.

Decision: EXISTING TASK
Reason: Reply to ongoing project thread
Search: threads with title containing "Website redesign"
```

### Ignore
```
Subject: This Week in Tech: AI Updates
From: newsletter@techcrunch.com

Decision: IGNORE
Reason: Newsletter from automated sender
```

---

*This memory is malleable - update these rules as patterns emerge.*

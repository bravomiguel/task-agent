# Email Triage Rules

This document contains the rules for filtering incoming emails to determine which should be processed as actionable tasks.

## Core Principle

**Only process emails that contain explicit action items for the user.**

An action item is a request, task, question, or deadline that requires the user to take action.

---

## Filter OUT (Reject)

### 1. Marketing & Promotional Emails
- Newsletters, product announcements, sales promotions
- Automated marketing campaigns
- Event invitations from commercial entities
- Keywords: "unsubscribe", "click here to view", "special offer", "limited time"

### 2. Automated Notifications
- System-generated notifications (e.g., "Password changed successfully")
- Social media notifications (likes, follows, comments)
- Automated receipts and confirmations (unless they require follow-up action)
- Delivery notifications, tracking updates
- Keywords: "no-reply@", "noreply@", "do not reply"

### 3. Spam & Junk
- Unsolicited commercial emails
- Phishing attempts
- Unknown senders with suspicious content
- Emails with excessive links or attachments from unknown sources

### 4. FYI-Only Emails
- Informational updates with no action required
- "Just keeping you in the loop" messages
- Status updates that don't require response
- Simple acknowledgments: "Got it", "Thanks", "Noted"
- Keywords: "FYI", "for your information", "no action required"

### 5. Social & Personal (No Action)
- Casual chat without requests
- Thank you notes (unless they include follow-up asks)
- Greetings and well-wishes
- Personal updates from friends/family (unless they ask for something)

---

## Process (Accept)

### 1. Explicit Requests
- Direct asks: "Can you...", "Could you...", "Please..."
- Questions that require answers: "What do you think about...", "How should we..."
- Delegation: "I need you to...", "Would you mind..."

### 2. Deadlines & Time-Sensitive Items
- Emails mentioning specific dates, deadlines, or time constraints
- Meeting requests or calendar invites requiring response
- Keywords: "by EOD", "deadline", "due date", "before [date]", "urgent"

### 3. Feedback Requests
- Requests for review, approval, or feedback
- "Can you review...", "Please take a look at...", "Thoughts on..."
- Document or presentation review requests

### 4. Assignments & Delegations
- Tasks explicitly assigned to the user
- Project assignments
- Work requests from managers or colleagues

### 5. Questions Requiring Response
- Direct questions addressed to the user
- Requests for information, clarification, or input
- Decision-making requests: "Should we...", "Which option..."

### 6. Follow-ups on Existing Work
- Emails referencing ongoing projects or tasks
- Updates that require the user to take next steps
- Questions about status or progress

---

## Action Item Detection Patterns

### Strong Indicators (Likely action item)
- **Imperative verbs**: "Please send", "Review this", "Let me know", "Update the"
- **Questions**: Emails ending with "?", especially when addressed to the user
- **Modal verbs of request**: "can you", "could you", "would you", "will you"
- **Deadlines**: Specific dates or time references
- **Attachments with review requests**: "Attached is...", "Please see attached"

### Weak Indicators (Possibly action item)
- CC'd on emails (might be FYI, check content)
- Forwarded emails (check if there's a request in the forward)
- Reply-all chains (check if user input is needed)

---

## Special Cases

### Emails from Boss/Manager
- **Default to PROCESS** unless clearly FYI-only
- Higher priority for action item detection

### Emails with Attachments
- Check if attachment requires review or action
- Standalone attachments without context = likely FYI, reject

### Calendar Invites
- **PROCESS** if user is required attendee and hasn't responded
- **REJECT** if already accepted/declined or FYI-only

### Out of Office Replies
- **REJECT** - automated responses, no action needed

---

## Examples

### ✅ PROCESS (Has Action Item)

1. "Can you send me the Q1 report by Friday?"
   - Explicit request + deadline

2. "What do you think about moving the meeting to next week?"
   - Question requiring response

3. "Please review the attached slides and let me know your thoughts."
   - Review request + feedback request

4. "Just following up on the presentation—did you get a chance to add those sections we discussed?"
   - Follow-up on existing work + implicit request

5. "Hey, I need your help with something. Can we chat later today?"
   - Explicit request for help

### ❌ REJECT (No Action Item)

1. "Thanks for your help yesterday!"
   - Simple acknowledgment, no request

2. "FYI, the meeting has been moved to 3pm."
   - Informational only, no action needed (assuming calendar was updated)

3. "[Newsletter] Top 10 Productivity Tips for 2024"
   - Marketing/newsletter

4. "Your password was successfully changed."
   - Automated notification

5. "Just wanted to keep you in the loop—the project is going well."
   - FYI-only update

---

## Confidence Levels

When analyzing emails, assess confidence:

- **High confidence (>0.9)**: Clear explicit request with deadline
- **Medium confidence (0.5-0.9)**: Question or implicit request
- **Low confidence (<0.5)**: Ambiguous, might be FYI

**Only process emails with medium or high confidence.**

---

## Thread Routing Rules

Once an email passes the filter and is determined to be an action item:

### Route to Existing Thread If:
1. Email references a specific ongoing project/task
2. Subject line contains keywords matching an active thread title
3. Sender has previous thread about same topic
4. Email explicitly replies to or follows up on existing work

### Create New Thread If:
1. New request unrelated to existing work
2. No keyword matches in active threads
3. Different topic from any active threads
4. First-time request from sender

**Priority**: Always prefer adding to existing thread over creating new one if there's >70% relevance match.

---

## Notes for Triage Agent

- **Read this file at the start of EVERY triage operation**
- Apply rules strictly—better to reject false positives than create noise
- When in doubt, use the "action item" test: Does this email require the user to DO something?
- Log rejected emails with reason for debugging
- For borderline cases, default to REJECT (conservative filtering)

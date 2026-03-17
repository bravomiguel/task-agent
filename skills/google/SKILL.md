---
name: google
description: Google Workspace CLI for Gmail, Calendar, Drive, Contacts, Sheets, and Docs via gog. Use this when you need to send emails, manage calendar events, access Google Drive, work with spreadsheets, or read Google Docs.
---

# Google Workspace (gog CLI)

## Authentication

**CRITICAL: Chain exports with `&&` in the SAME execute call.** Env vars don't persist across separate execute calls.

```bash
export GOG_ACCESS_TOKEN=$(cat /workspace/.auth/google_token) && export GOG_ACCOUNT=me && gog gmail send --to recipient@example.com --subject "Hello" --body "Message"
```

`GOG_ACCOUNT=me` is required when using access tokens. Always prefix every gog command with both exports.

## Gmail

```bash
# Search threads (default: last 7 days)
export GOG_ACCESS_TOKEN=$(cat /workspace/.auth/google_token) && export GOG_ACCOUNT=me && gog gmail search 'newer_than:7d' --max 10

# Search individual messages (not grouped by thread)
gog gmail messages search "in:inbox from:sender@example.com" --max 20

# Send plain text
gog gmail send --to recipient@example.com --subject "Subject" --body "Hello"

# Send multi-line (via file or stdin)
gog gmail send --to recipient@example.com --subject "Subject" --body-file ./message.txt
gog gmail send --to recipient@example.com --subject "Subject" --body-file - <<'EOF'
Hi,

Message body here.

Best,
Name
EOF

# Send HTML
gog gmail send --to recipient@example.com --subject "Subject" --body-html "<p>Hello</p>"

# Create draft
gog gmail drafts create --to recipient@example.com --subject "Subject" --body-file ./message.txt

# Send existing draft
gog gmail drafts send <draftId>

# Reply to a message
gog gmail send --to recipient@example.com --subject "Re: Original" --body "Reply" --reply-to-message-id <msgId>
```

## Calendar

```bash
# List events
gog calendar events <calendarId> --from <iso> --to <iso>

# Create event
gog calendar create <calendarId> --summary "Title" --from <iso> --to <iso>

# Create with color
gog calendar create <calendarId> --summary "Title" --from <iso> --to <iso> --event-color 7

# Update event
gog calendar update <calendarId> <eventId> --summary "New Title" --event-color 4

# Show available colors
gog calendar colors
```

Event color IDs: 1=#a4bdfc, 2=#7ae7bf, 3=#dbadff, 4=#ff887c, 5=#fbd75b, 6=#ffb878, 7=#46d6db, 8=#e1e1e1, 9=#5484ed, 10=#51b749, 11=#dc2127

## Drive

```bash
gog drive search "query" --max 10
```

## Contacts

```bash
gog contacts list --max 20
```

## Sheets

```bash
# Read cells
gog sheets get <sheetId> "Tab!A1:D10" --json

# Write cells
gog sheets update <sheetId> "Tab!A1:B2" --values-json '[["A","B"],["1","2"]]' --input USER_ENTERED

# Append rows
gog sheets append <sheetId> "Tab!A:C" --values-json '[["x","y","z"]]' --insert INSERT_ROWS

# Clear range
gog sheets clear <sheetId> "Tab!A2:Z"

# Sheet metadata
gog sheets metadata <sheetId> --json
```

## Docs

```bash
# Export to text
gog docs export <docId> --format txt --out /tmp/doc.txt

# Print to stdout
gog docs cat <docId>
```

## Email Formatting

- Prefer plain text. Use `--body-file` for multi-paragraph messages (or `--body-file -` for stdin).
- Same `--body-file` pattern works for drafts and replies.
- `--body` does not unescape `\n`. Use a heredoc or `$'Line 1\n\nLine 2'` for inline newlines.
- Use `--body-html` only when rich formatting is needed.
- HTML tags: `<p>` paragraphs, `<br>` line breaks, `<strong>` bold, `<em>` italic, `<a href="url">` links, `<ul>`/`<li>` lists.

Example (plain text via stdin):

```bash
gog gmail send --to recipient@example.com \
  --subject "Meeting Follow-up" \
  --body-file - <<'EOF'
Hi Name,

Thanks for meeting today. Next steps:
- Item one
- Item two

Best regards,
Your Name
EOF
```

Example (HTML):

```bash
gog gmail send --to recipient@example.com \
  --subject "Meeting Follow-up" \
  --body-html "<p>Hi Name,</p><p>Next steps:</p><ul><li>Item one</li><li>Item two</li></ul><p>Best regards,<br>Your Name</p>"
```

## Tips

- Set `GOG_ACCOUNT=you@gmail.com` to avoid repeating `--account`.
- Use `--json` and `--no-input` for scripting.
- Sheets values can be passed via `--values-json` (recommended) or as inline rows.
- Docs supports export/cat/copy. In-place edits require a Docs API client (not in gog).
- Confirm with the user before sending mail or creating events.
- `gog gmail search` groups by thread; `gog gmail messages search` returns individual messages.

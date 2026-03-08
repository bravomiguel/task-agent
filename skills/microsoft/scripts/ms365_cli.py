#!/usr/bin/env python3
"""MS365 CLI — Microsoft 365 via Graph API.

Reads Bearer token from /workspace/.auth/microsoft_token
(written by manage_auth connect microsoft).

Covers: Mail (Outlook), Calendar, Contacts.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH_URL = "https://graph.microsoft.com/v1.0"
TOKEN_FILE = "/workspace/.auth/microsoft_token"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        print(
            json.dumps({"error": f"Token not found at {TOKEN_FILE}. Run: manage_auth connect microsoft"}),
            file=sys.stderr,
        )
        sys.exit(1)


def _graph(method: str, path: str, body: dict = None, params: dict = None) -> dict:
    """Make a Microsoft Graph API call."""
    token = _get_token()
    url = f"{GRAPH_URL}{path}"
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 202 or resp.status == 204:
                return {"status": "success"}
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        try:
            return {"error": json.loads(error_body)}
        except (json.JSONDecodeError, ValueError):
            return {"error": f"HTTP {e.code}: {error_body[:500]}"}


def _out(data: dict) -> None:
    print(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

def cmd_user(_args: argparse.Namespace) -> None:
    """Get current user info."""
    _out(_graph("GET", "/me"))


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

def cmd_mail_list(args: argparse.Namespace) -> None:
    """List emails."""
    params: dict = {"$top": str(args.top), "$orderby": "receivedDateTime desc"}
    if args.folder:
        path = f"/me/mailFolders/{args.folder}/messages"
    else:
        path = "/me/messages"
    params["$select"] = "id,subject,from,receivedDateTime,isRead,hasAttachments,bodyPreview"
    result = _graph("GET", path, params=params)
    messages = result.get("value", [])
    _out(messages)


def cmd_mail_read(args: argparse.Namespace) -> None:
    """Read a specific email."""
    _out(_graph("GET", f"/me/messages/{args.id}"))


def cmd_mail_send(args: argparse.Namespace) -> None:
    """Send an email."""
    recipients = [{"emailAddress": {"address": addr.strip()}} for addr in args.to.split(",")]
    body = {
        "message": {
            "subject": args.subject,
            "body": {"contentType": "Text", "content": args.body},
            "toRecipients": recipients,
        }
    }
    if args.cc:
        body["message"]["ccRecipients"] = [
            {"emailAddress": {"address": addr.strip()}} for addr in args.cc.split(",")
        ]
    _out(_graph("POST", "/me/sendMail", body=body))


def cmd_mail_search(args: argparse.Namespace) -> None:
    """Search emails."""
    params = {
        "$search": f'"{args.query}"',
        "$top": str(args.top),
        "$select": "id,subject,from,receivedDateTime,bodyPreview",
    }
    result = _graph("GET", "/me/messages", params=params)
    _out(result.get("value", []))


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def cmd_calendar_list(args: argparse.Namespace) -> None:
    """List upcoming calendar events."""
    params = {
        "$top": str(args.top),
        "$orderby": "start/dateTime",
        "$select": "id,subject,start,end,location,organizer,isAllDay,bodyPreview",
    }
    _out(_graph("GET", "/me/events", params=params))


def cmd_calendar_create(args: argparse.Namespace) -> None:
    """Create a calendar event."""
    tz = args.timezone or "UTC"
    body: dict = {
        "subject": args.subject,
        "start": {"dateTime": args.start, "timeZone": tz},
        "end": {"dateTime": args.end, "timeZone": tz},
    }
    if args.body:
        body["body"] = {"contentType": "Text", "content": args.body}
    if args.location:
        body["location"] = {"displayName": args.location}
    if args.attendees:
        body["attendees"] = [
            {"emailAddress": {"address": addr.strip()}, "type": "required"}
            for addr in args.attendees.split(",")
        ]
    _out(_graph("POST", "/me/events", body=body))


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def cmd_contacts_list(args: argparse.Namespace) -> None:
    """List contacts."""
    params = {
        "$top": str(args.top),
        "$select": "id,displayName,emailAddresses,businessPhones,companyName,jobTitle",
    }
    result = _graph("GET", "/me/contacts", params=params)
    _out(result.get("value", []))


def cmd_contacts_search(args: argparse.Namespace) -> None:
    """Search people."""
    params = {"$search": f'"{args.query}"', "$top": str(args.top)}
    result = _graph("GET", "/me/people", params=params)
    _out(result.get("value", []))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="MS365 CLI — Microsoft 365 via Graph API")
    sub = parser.add_subparsers(dest="command")

    # User
    p_user = sub.add_parser("user", help="Get current user info")
    p_user.set_defaults(func=cmd_user)

    # Mail
    p_mail = sub.add_parser("mail", help="Mail commands")
    mail_sub = p_mail.add_subparsers(dest="mail_cmd")

    p_ml = mail_sub.add_parser("list", help="List emails")
    p_ml.add_argument("--top", type=int, default=10)
    p_ml.add_argument("--folder", help="Mail folder ID")
    p_ml.set_defaults(func=cmd_mail_list)

    p_mr = mail_sub.add_parser("read", help="Read email")
    p_mr.add_argument("id", help="Message ID")
    p_mr.set_defaults(func=cmd_mail_read)

    p_ms = mail_sub.add_parser("send", help="Send email")
    p_ms.add_argument("--to", required=True, help="Recipient(s), comma-separated")
    p_ms.add_argument("--cc", help="CC recipient(s), comma-separated")
    p_ms.add_argument("--subject", required=True)
    p_ms.add_argument("--body", required=True)
    p_ms.set_defaults(func=cmd_mail_send)

    p_mq = mail_sub.add_parser("search", help="Search emails")
    p_mq.add_argument("query", help="Search query")
    p_mq.add_argument("--top", type=int, default=10)
    p_mq.set_defaults(func=cmd_mail_search)

    # Calendar
    p_cal = sub.add_parser("calendar", help="Calendar commands")
    cal_sub = p_cal.add_subparsers(dest="cal_cmd")

    p_cl = cal_sub.add_parser("list", help="List upcoming events")
    p_cl.add_argument("--top", type=int, default=10)
    p_cl.set_defaults(func=cmd_calendar_list)

    p_cc = cal_sub.add_parser("create", help="Create event")
    p_cc.add_argument("--subject", required=True)
    p_cc.add_argument("--start", required=True, help="ISO datetime (e.g. 2026-03-15T10:00:00)")
    p_cc.add_argument("--end", required=True, help="ISO datetime")
    p_cc.add_argument("--body", help="Event description")
    p_cc.add_argument("--location", help="Location name")
    p_cc.add_argument("--timezone", default="UTC")
    p_cc.add_argument("--attendees", help="Attendee emails, comma-separated")
    p_cc.set_defaults(func=cmd_calendar_create)

    # Contacts
    p_con = sub.add_parser("contacts", help="Contacts commands")
    con_sub = p_con.add_subparsers(dest="con_cmd")

    p_col = con_sub.add_parser("list", help="List contacts")
    p_col.add_argument("--top", type=int, default=20)
    p_col.set_defaults(func=cmd_contacts_list)

    p_cos = con_sub.add_parser("search", help="Search people")
    p_cos.add_argument("query", help="Search query")
    p_cos.add_argument("--top", type=int, default=10)
    p_cos.set_defaults(func=cmd_contacts_search)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

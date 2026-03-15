#!/usr/bin/env python3
"""MS365 CLI — Microsoft 365 via Graph API.

Reads Bearer token from /workspace/.auth/microsoft_token
(written by python3 /mnt/scripts/fetch_auth.py microsoft).

Covers: Mail (Outlook), Calendar, OneDrive, To Do, Contacts.
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
            json.dumps({"error": f"Token not found at {TOKEN_FILE}. Run: python3 /mnt/scripts/fetch_auth.py microsoft"}),
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
# OneDrive Files
# ---------------------------------------------------------------------------

def cmd_files_list(args: argparse.Namespace) -> None:
    """List files in OneDrive."""
    if args.path:
        path = f"/me/drive/root:/{args.path}:/children"
    else:
        path = "/me/drive/root/children"
    params = {
        "$top": str(args.top),
        "$select": "id,name,size,lastModifiedDateTime,folder,file,webUrl",
    }
    result = _graph("GET", path, params=params)
    _out(result.get("value", []))


def cmd_files_get(args: argparse.Namespace) -> None:
    """Get file metadata."""
    _out(_graph("GET", f"/me/drive/items/{args.id}"))


def cmd_files_search(args: argparse.Namespace) -> None:
    """Search files in OneDrive."""
    params = {"$top": str(args.top)}
    result = _graph("GET", f"/me/drive/root/search(q='{args.query}')", params=params)
    _out(result.get("value", []))


def cmd_files_download(args: argparse.Namespace) -> None:
    """Download a file from OneDrive to local path."""
    # Get download URL
    meta = _graph("GET", f"/me/drive/items/{args.id}")
    download_url = meta.get("@microsoft.graph.downloadUrl")
    if not download_url:
        _out({"error": "Could not get download URL", "metadata": meta})
        return

    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
        output_path = args.output or meta.get("name", "downloaded_file")
        with open(output_path, "wb") as f:
            f.write(content)
        _out({"status": "downloaded", "path": output_path, "size": len(content)})
    except Exception as e:
        _out({"error": str(e)})


# ---------------------------------------------------------------------------
# To Do Tasks
# ---------------------------------------------------------------------------

def cmd_tasks_lists(_args: argparse.Namespace) -> None:
    """List To Do task lists."""
    result = _graph("GET", "/me/todo/lists")
    _out(result.get("value", []))


def cmd_tasks_get(args: argparse.Namespace) -> None:
    """Get tasks from a list."""
    params = {"$top": str(args.top)}
    result = _graph("GET", f"/me/todo/lists/{args.list_id}/tasks", params=params)
    _out(result.get("value", []))


def cmd_tasks_create(args: argparse.Namespace) -> None:
    """Create a new task."""
    body: dict = {"title": args.title}
    if args.due:
        body["dueDateTime"] = {"dateTime": f"{args.due}T00:00:00", "timeZone": "UTC"}
    _out(_graph("POST", f"/me/todo/lists/{args.list_id}/tasks", body=body))


def cmd_tasks_complete(args: argparse.Namespace) -> None:
    """Mark a task as complete."""
    _out(_graph("PATCH", f"/me/todo/lists/{args.list_id}/tasks/{args.task_id}", body={"status": "completed"}))


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

    # Files (OneDrive)
    p_files = sub.add_parser("files", help="OneDrive commands")
    files_sub = p_files.add_subparsers(dest="files_cmd")

    p_fl = files_sub.add_parser("list", help="List files")
    p_fl.add_argument("--path", help="Folder path (e.g. Documents/Reports)")
    p_fl.add_argument("--top", type=int, default=20)
    p_fl.set_defaults(func=cmd_files_list)

    p_fg = files_sub.add_parser("get", help="Get file metadata")
    p_fg.add_argument("id", help="File/item ID")
    p_fg.set_defaults(func=cmd_files_get)

    p_fs = files_sub.add_parser("search", help="Search files")
    p_fs.add_argument("query", help="Search query")
    p_fs.add_argument("--top", type=int, default=10)
    p_fs.set_defaults(func=cmd_files_search)

    p_fd = files_sub.add_parser("download", help="Download a file")
    p_fd.add_argument("id", help="File item ID")
    p_fd.add_argument("--output", help="Local output path")
    p_fd.set_defaults(func=cmd_files_download)

    # Tasks (To Do)
    p_tasks = sub.add_parser("tasks", help="To Do commands")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_cmd")

    p_tl = tasks_sub.add_parser("lists", help="List task lists")
    p_tl.set_defaults(func=cmd_tasks_lists)

    p_tg = tasks_sub.add_parser("get", help="Get tasks from list")
    p_tg.add_argument("list_id", help="Task list ID")
    p_tg.add_argument("--top", type=int, default=20)
    p_tg.set_defaults(func=cmd_tasks_get)

    p_tc = tasks_sub.add_parser("create", help="Create task")
    p_tc.add_argument("list_id", help="Task list ID")
    p_tc.add_argument("--title", required=True)
    p_tc.add_argument("--due", help="Due date (YYYY-MM-DD)")
    p_tc.set_defaults(func=cmd_tasks_create)

    p_td = tasks_sub.add_parser("complete", help="Mark task complete")
    p_td.add_argument("list_id", help="Task list ID")
    p_td.add_argument("task_id", help="Task ID")
    p_td.set_defaults(func=cmd_tasks_complete)

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

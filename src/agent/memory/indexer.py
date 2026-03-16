"""Orchestrator for memory index sync.

Lists memory, session-transcript, and meeting-transcript files inside the
Modal sandbox, reads their content, then delegates chunking + embedding +
storage to the Supabase-backed store module.

Meeting transcripts are synced from Supabase Storage (uploaded by the
Electron meeting-recorder app) to the volume before indexing.
"""

from __future__ import annotations

import json
import logging
import os
import time

import modal

from agent.memory.store import MEMORY_DIR, MEETINGS_DIR, SESSIONS_DIR, sync_memory_chunks

logger = logging.getLogger(__name__)

# Script piped into the sandbox to list .md files with metadata + content.
# Returns JSON array of {path, mtime, size, content}.
_LIST_FILES_SCRIPT = r"""
import json, os, sys
from pathlib import Path

DIRS = ["{memory_dir}", "{sessions_dir}", "{meetings_dir}"]
files = []
for d in DIRS:
    dp = Path(d)
    if not dp.is_dir():
        continue
    for f in sorted(dp.iterdir()):
        if f.name.startswith(".") or not f.is_file() or f.suffix != ".md":
            continue
        try:
            stat = f.stat()
            content = f.read_text(encoding="utf-8", errors="replace")
            files.append({{"path": str(f), "mtime": stat.st_mtime, "size": stat.st_size, "content": content}})
        except Exception:
            pass
json.dump(files, sys.stdout)
""".format(memory_dir=MEMORY_DIR, sessions_dir=SESSIONS_DIR, meetings_dir=MEETINGS_DIR)

# Script to sync meeting transcripts from Supabase Storage to the volume.
# Lists objects in the bucket, compares with local files, downloads new ones.
_SYNC_MEETINGS_SCRIPT = r"""
import json, os, sys, urllib.request, urllib.error

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
USER_ID = os.environ.get("USER_ID", "")
BUCKET = "meeting-transcripts"
LOCAL_DIR = "{meetings_dir}"

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY or not USER_ID:
    json.dump({{"synced": 0, "skipped": "missing env"}}, sys.stdout)
    sys.exit(0)

os.makedirs(LOCAL_DIR, exist_ok=True)

# List objects in the user's folder in the bucket
headers = {{
    "Authorization": f"Bearer {{SUPABASE_SERVICE_KEY}}",
    "apikey": SUPABASE_SERVICE_KEY,
}}

try:
    list_url = f"{{SUPABASE_URL}}/storage/v1/object/list/{{BUCKET}}"
    body = json.dumps({{"prefix": f"{{USER_ID}}/", "limit": 200}}).encode()
    req = urllib.request.Request(list_url, data=body, headers={{**headers, "Content-Type": "application/json"}}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        objects = json.loads(resp.read())
except Exception as e:
    json.dump({{"synced": 0, "error": str(e)}}, sys.stdout)
    sys.exit(0)

# Download new files
synced = 0
for obj in objects:
    name = obj.get("name", "")
    if not name or not name.endswith(".md"):
        continue
    # name is "user-id/filename.md" — extract just filename
    filename = name.split("/")[-1]
    local_path = os.path.join(LOCAL_DIR, filename)

    # Skip if already exists and same size
    remote_size = obj.get("metadata", {{}}).get("size", 0)
    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        if local_size > 0 and (remote_size == 0 or local_size == remote_size):
            continue

    # Download
    try:
        dl_url = f"{{SUPABASE_URL}}/storage/v1/object/{{BUCKET}}/{{name}}"
        req = urllib.request.Request(dl_url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read()
        with open(local_path, "wb") as f:
            f.write(content)
        synced += 1
    except Exception as e:
        print(f"Failed to download {{name}}: {{e}}", file=sys.stderr)

json.dump({{"synced": synced, "total": len(objects)}}, sys.stdout)
""".format(meetings_dir=MEETINGS_DIR)


def _sync_meeting_transcripts_from_storage(sandbox: modal.Sandbox) -> dict:
    """Download new meeting transcripts from Supabase Storage to the volume."""
    try:
        process = sandbox.exec("python3", "-c", _SYNC_MEETINGS_SCRIPT, timeout=60)
        process.wait()

        stdout = process.stdout.read()
        stderr = process.stderr.read()

        if stderr:
            logger.info("[MemoryIndex] meeting sync stderr: %s", stderr[:500])

        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            result = {"synced": 0, "error": "parse failed"}

        if result.get("synced", 0) > 0:
            logger.info("[MemoryIndex] synced %d meeting transcripts from Storage", result["synced"])

        return result
    except Exception as exc:
        logger.warning("[MemoryIndex] meeting transcript sync error: %s", exc)
        return {"synced": 0, "error": str(exc)}


def sync_memory_index(sandbox: modal.Sandbox) -> dict:
    """Run incremental memory-index sync.

    1. Syncs meeting transcripts from Supabase Storage to the volume.
    2. Lists .md files inside the sandbox (where the volume is mounted).
    3. Reads file content from sandbox.
    4. Delegates to store.sync_memory_chunks for chunking, embedding,
       and Supabase upsert.
    """
    try:
        t0 = time.monotonic()

        # Sync meeting transcripts from Supabase Storage → volume
        _sync_meeting_transcripts_from_storage(sandbox)

        # List and read all .md files from sandbox
        t1 = time.monotonic()
        process = sandbox.exec("python3", "-c", _LIST_FILES_SCRIPT, timeout=60)
        process.wait()

        stdout = process.stdout.read()
        stderr = process.stderr.read()

        if stderr:
            logger.info("[MemoryIndex] list stderr: %s", stderr[:500])

        if process.returncode != 0:
            logger.warning(
                "[MemoryIndex] file listing failed (rc=%d): %s",
                process.returncode,
                stderr[:500],
            )
            return {"status": "error", "message": stderr[:500]}

        try:
            file_list = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "[MemoryIndex] could not parse file listing: %s", stdout[:500]
            )
            return {"status": "error", "message": "Failed to parse file listing"}

        logger.info(
            "[MemoryIndex] found %d files in %.1fs",
            len(file_list),
            time.monotonic() - t1,
        )

        # Convert to the format expected by sync_memory_chunks
        files: dict[str, dict] = {}
        for f in file_list:
            files[f["path"]] = {
                "mtime": f["mtime"],
                "size": f["size"],
                "content": f["content"],
            }

        # Sync to Supabase
        t2 = time.monotonic()
        result = sync_memory_chunks(files)
        logger.info(
            "[MemoryIndex] sync result: %s (%.1fs)",
            result,
            time.monotonic() - t2,
        )

        logger.info(
            "[MemoryIndex] total sync time: %.1fs", time.monotonic() - t0
        )
        return result

    except Exception as exc:
        logger.warning("[MemoryIndex] sync error: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}

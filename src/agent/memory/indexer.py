"""Orchestrator for memory index sync.

Lists memory, session-transcript, and meeting-transcript files inside the
Modal sandbox, reads their content, then delegates chunking + embedding +
storage to the Supabase-backed store module.

Meeting transcripts are primarily indexed by the Electron app at write time.
This indexer serves as a fallback — files already indexed (matching content
hash) are skipped at near-zero cost.
"""

from __future__ import annotations

import json
import logging
import time

import modal

from agent.memory.store import MEETINGS_DIR, MEMORY_DIR, SESSIONS_DIR, sync_memory_chunks

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


def sync_memory_index(sandbox: modal.Sandbox) -> dict:
    """Run incremental memory-index sync.

    1. Lists .md files inside the sandbox (where the volume is mounted).
    2. Reads file content from sandbox.
    3. Delegates to store.sync_memory_chunks for chunking, embedding,
       and Supabase upsert.
    """
    try:
        t0 = time.monotonic()

        # Skip reload_volumes() — the sandbox was just created with the volume
        # mounted, so it already has the latest state.  reload_volumes() causes
        # the volume to appear EMPTY while in progress, which is why the file
        # listing was returning 0 files.

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

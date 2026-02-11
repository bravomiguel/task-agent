"""Orchestrator for memory index sync — runs indexing inside Modal sandbox.

The actual LanceDB operations happen inside the sandbox (where the volume is
mounted).  This module loads the self-contained _sandbox_script.py, pipes it
to ``python3 -`` via sandbox.exec(), and parses the JSON result.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import modal

logger = logging.getLogger(__name__)

_SCRIPT_PATH = Path(__file__).parent / "_sandbox_script.py"
_script_cache: str | None = None


def _load_script() -> str:
    global _script_cache
    if _script_cache is None:
        _script_cache = _SCRIPT_PATH.read_text()
    return _script_cache


def sync_memory_index(sandbox: modal.Sandbox) -> dict:
    """Run incremental memory-index sync inside the Modal sandbox.

    1. Pipes _sandbox_script.py to ``python3 -`` with the ``sync`` subcommand.
    2. The script lists ``/default-user/memory/*.md``, checks mtime/size
       against what is already indexed, and only re-embeds changed files.
    3. Returns the JSON summary emitted by the script.
    """
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning("[MemoryIndex] No OPENAI_API_KEY — skipping sync")
        return {"status": "skipped", "message": "No API key"}

    script = _load_script()
    logger.info("[MemoryIndex] script loaded (%d bytes)", len(script))

    try:
        t0 = time.monotonic()
        logger.info("[MemoryIndex] reloading volumes…")
        sandbox.reload_volumes()
        logger.info("[MemoryIndex] volumes reloaded in %.1fs", time.monotonic() - t0)

        t1 = time.monotonic()
        logger.info("[MemoryIndex] starting sandbox exec (python3 - sync)…")
        process = sandbox.exec(
            "python3", "-", "sync", "--api-key", api_key,
            timeout=120,
        )
        logger.info("[MemoryIndex] exec started in %.1fs, writing stdin…", time.monotonic() - t1)

        t2 = time.monotonic()
        process.stdin.write(script.encode())
        process.stdin.write_eof()
        process.stdin.drain()
        logger.info("[MemoryIndex] stdin written+drained in %.2fs, waiting for process…", time.monotonic() - t2)

        t3 = time.monotonic()
        process.wait()
        logger.info(
            "[MemoryIndex] process finished in %.1fs (rc=%s)",
            time.monotonic() - t3, process.returncode,
        )

        stdout = process.stdout.read()
        stderr = process.stderr.read()

        if stderr:
            logger.info("[MemoryIndex] sync stderr: %s", stderr[:1000])

        if process.returncode != 0:
            logger.warning(
                "[MemoryIndex] sync failed (rc=%d): %s",
                process.returncode, stderr[:500],
            )
            return {"status": "error", "message": stderr[:500]}

        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            logger.warning("[MemoryIndex] could not parse stdout: %s", stdout[:500])
            result = {"status": "ok", "raw": stdout[:500]}

        logger.info("[MemoryIndex] sync result: %s (total %.1fs)", result, time.monotonic() - t0)
        return result

    except Exception as exc:
        logger.warning("[MemoryIndex] sync error: %s", exc, exc_info=True)
        return {"status": "error", "message": str(exc)}

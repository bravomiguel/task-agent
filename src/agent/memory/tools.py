"""Memory search tool for the agent.

Executes a hybrid (BM25 + vector) search against the LanceDB memory index
inside the Modal sandbox and returns raw JSON results.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated

import modal
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

logger = logging.getLogger(__name__)

_SCRIPT_PATH = Path(__file__).parent / "_sandbox_script.py"
_script_cache: str | None = None


def _load_script() -> str:
    global _script_cache
    if _script_cache is None:
        _script_cache = _SCRIPT_PATH.read_text()
    return _script_cache


@tool
def memory_search(
    query: str,
    max_results: int = 6,
    min_score: float = 0.35,
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Mandatory recall step: semantically search MEMORY.md + memory/*.md before answering questions about prior work, decisions, dates, people, preferences, or todos; returns top snippets with path + lines.

    Args:
        query: Natural language description of what you're looking for.
        max_results: Maximum number of results to return (default: 6).
        min_score: Minimum relevance score threshold 0-1 (default: 0.35).

    Returns:
        JSON with results array containing path, startLine, endLine, score, snippet, source.
        Use read_file to get full context for any relevant result.
    """
    if state is None:
        return "Error: Could not access state."

    sandbox_id = state.get("modal_sandbox_id")
    if not sandbox_id:
        return "Error: No sandbox available."

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return "Error: No OpenAI API key available for embedding."

    script = _load_script()

    try:
        sandbox = modal.Sandbox.from_id(sandbox_id)
        sandbox.reload_volumes()

        process = sandbox.exec(
            "python3", "-", "search",
            "--query", query,
            "--max-results", str(max_results),
            "--min-score", str(min_score),
            "--api-key", api_key,
            timeout=30,
        )
        process.stdin.write(script.encode())
        process.stdin.write_eof()
        process.stdin.drain()
        process.wait()

        stdout = process.stdout.read()
        stderr = process.stderr.read()

        if process.returncode != 0:
            logger.warning("[MemorySearch] failed: %s", stderr[:500])
            return f"Error searching memory: {stderr[:200]}"

        return stdout

    except Exception as exc:
        logger.warning("[MemorySearch] error: %s", exc)
        return f"Error searching memory: {exc}"

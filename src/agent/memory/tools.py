"""Memory search tool for the agent.

Executes a hybrid (BM25 + vector) search against the LanceDB memory index
inside the Modal sandbox and returns formatted results.
"""

from __future__ import annotations

import json
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
    state: Annotated[dict, InjectedState] = None,
) -> str:
    """Search your memory (daily logs and long-term notes) for relevant context.

    Use this to recall information from previous sessions — past conversations,
    decisions, user preferences, and anything captured in your memory files.

    Args:
        query: Natural language description of what you're looking for.
        max_results: Maximum number of results to return (default: 6).

    Returns:
        Matching snippets with file paths and line numbers.
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

        data = json.loads(stdout)
        results = data.get("results", [])

        if not results:
            return "No matching memories found."

        lines = [f"Found {len(results)} result(s):\n"]
        for i, r in enumerate(results, 1):
            snippet = r["snippet"][:700]
            lines.append(
                f'{i}. {r["path"]} (lines {r["start_line"]}-{r["end_line"]}, '
                f'score: {r["score"]})\n   "{snippet}..."\n'
            )

        return "\n".join(lines)

    except Exception as exc:
        logger.warning("[MemorySearch] error: %s", exc)
        return f"Error searching memory: {exc}"

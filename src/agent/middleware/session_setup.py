"""Session setup middleware — runs agents prompt, skills, and memory setup concurrently.

Replaces the sequential before_agent hooks of AgentsPromptMiddleware,
SkillsMiddleware, and MemoryMiddleware with a single hook that runs all
three operations in parallel via asyncio.gather / ThreadPoolExecutor.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime

from agent.middleware.memory import (
    LANGGRAPH_API_URL,
    _afind_previous_session,
    _amark_session_archived,
    _build_archive_content,
    _extract_conversation_text,
    _generate_slug,
    _write_archive_to_volume,
)
from agent.middleware.skills import _list_skills_from_sandbox

logger = logging.getLogger(__name__)

class SessionSetupMiddleware(AgentMiddleware[AgentState, Any]):
    """Runs prompt file loading, skills discovery, and memory setup in parallel.

    Loads all .md files from /default-user/prompts/ plus /default-user/memory/MEMORY.md
    as project context files (mirroring OpenClaw's bootstrap file pattern).
    """

    def __init__(
        self,
        llm: Any = None,
        api_url: str | None = None,
        skills_path: str = "/default-user/skills",
        archive_message_limit: int = 15,
    ):
        super().__init__()
        self._llm = llm
        self._api_url = api_url or LANGGRAPH_API_URL
        self._skills_path = skills_path
        self._archive_message_limit = archive_message_limit

    # -- Prompt files (sync, runs in thread) ---------------------------------

    def _load_prompt_files(self, sandbox_id: str) -> dict[str, Any]:
        """Read all .md files from /default-user/prompts/ + MEMORY.md in one sandbox call."""
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            process = sandbox.exec(
                "bash", "-c",
                'for f in /default-user/prompts/*.md; do '
                '[ -f "$f" ] && echo "---FILE:$(basename $f)" && cat "$f"; '
                'done; '
                '[ -f /default-user/memory/MEMORY.md ] && '
                'echo "---FILE:MEMORY.md" && cat /default-user/memory/MEMORY.md; '
                'true',
                timeout=10,
            )
            process.wait()

            stdout = process.stdout.read()
            prompt_files: dict[str, str] = {}
            current_name: str | None = None
            current_lines: list[str] = []

            for line in stdout.split("\n"):
                if line.startswith("---FILE:"):
                    if current_name and current_lines:
                        content = "\n".join(current_lines).strip()
                        if content:
                            prompt_files[current_name] = content
                    current_name = line[len("---FILE:"):]
                    current_lines = []
                else:
                    current_lines.append(line)

            if current_name and current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    prompt_files[current_name] = content

            return {"prompt_files": prompt_files}
        except Exception as e:
            logger.warning("[SessionSetup] failed to load prompt files: %s", e)
            return {}

    # -- Skills discovery (sync, runs in thread) -----------------------------

    def _load_skills(self, sandbox_id: str) -> dict[str, Any]:
        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            skills = _list_skills_from_sandbox(sandbox, self._skills_path)
            return {"skills_metadata": skills}
        except Exception:
            return {}

    # -- Memory setup --------------------------------------------------------

    def _start_memory_index_sync(self, sandbox_id: str) -> None:
        from agent.memory.indexer import sync_memory_index

        def _bg_sync():
            try:
                import time as _time

                logger.info(
                    "[MemoryIndex] background sync starting (sandbox=%s)",
                    sandbox_id,
                )
                t0 = _time.monotonic()
                sb = modal.Sandbox.from_id(sandbox_id)
                sync_memory_index(sb)
                logger.info(
                    "[MemoryIndex] background sync completed in %.1fs",
                    _time.monotonic() - t0,
                )
            except Exception as e:
                logger.warning("[MemoryIndex] background sync failed: %s", e)

        threading.Thread(target=_bg_sync, daemon=True).start()

    async def _archive_previous_session(
        self, state: dict, sandbox_id: str
    ) -> None:
        current_session_id = state.get("session_id")
        if not current_session_id or not self._llm:
            return

        prev_session = await _afind_previous_session(
            current_session_id, self._api_url
        )
        if not prev_session:
            return

        prev_session_id = prev_session["thread_id"]  # LangGraph API key
        values = prev_session.get("values") or {}
        messages = values.get("messages", [])

        conversation_text = _extract_conversation_text(
            messages, limit=self._archive_message_limit
        )
        if not conversation_text:
            await _amark_session_archived(prev_session_id, self._api_url)
            return

        slug = _generate_slug(self._llm, conversation_text)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{date_str}-{slug}.md"
        content = _build_archive_content(prev_session_id, conversation_text)

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            _write_archive_to_volume(sandbox, filename, content)
        except Exception as e:
            logger.warning("[SessionSetup] failed to write archive: %s", e)
            return

        await _amark_session_archived(prev_session_id, self._api_url)

    async def _setup_memory(self, state: dict) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        sandbox_id = state.get("modal_sandbox_id")

        if state.get("session_type", "main") == "main" and sandbox_id:
            try:
                await self._archive_previous_session(state, sandbox_id)
            except Exception as e:
                logger.warning("[SessionSetup] archive error: %s", e)

        if sandbox_id and not state.get("_memory_index_synced"):
            self._start_memory_index_sync(sandbox_id)
            updates["_memory_index_synced"] = True

        return updates

    # -- Hooks ---------------------------------------------------------------

    def before_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Sync: prompt files + skills in parallel. Archive skipped (needs async)."""
        updates: dict[str, Any] = {}

        if not state.get("session_type"):
            updates["session_type"] = "main"

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return updates or None

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(self._load_prompt_files, sandbox_id),
                pool.submit(self._load_skills, sandbox_id),
            ]
            for future in futures:
                try:
                    result = future.result(timeout=60)
                    if result:
                        updates.update(result)
                except Exception as e:
                    logger.warning("[SessionSetup] task failed: %s", e)

        if not state.get("_memory_index_synced"):
            self._start_memory_index_sync(sandbox_id)
            updates["_memory_index_synced"] = True

        return updates or None

    async def abefore_agent(
        self, state: AgentState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async: all three operations concurrently."""
        updates: dict[str, Any] = {}

        if not state.get("session_type"):
            updates["session_type"] = "main"

        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return updates or None

        loop = asyncio.get_running_loop()

        results = await asyncio.gather(
            loop.run_in_executor(None, self._load_prompt_files, sandbox_id),
            loop.run_in_executor(None, self._load_skills, sandbox_id),
            self._setup_memory(state),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning("[SessionSetup] task failed: %s", result)
            elif isinstance(result, dict):
                updates.update(result)

        return updates or None

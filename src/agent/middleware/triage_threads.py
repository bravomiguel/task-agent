"""Triage threads middleware for fetching and dumping active threads."""

from __future__ import annotations

import json
import os
from typing import Any, NotRequired

import modal
import httpx
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime


# LangGraph API URL
LANGGRAPH_API_URL = os.getenv("LANGGRAPH_API_URL", "http://localhost:2024")


class TriageThreadsState(AgentState):
    """State schema for triage threads middleware."""
    modal_sandbox_id: NotRequired[str]
    active_thread_count: NotRequired[int]


class TriageThreadsMiddleware(AgentMiddleware[TriageThreadsState, Any]):
    """Middleware that fetches threads and dumps active ones to sandbox.

    Must run AFTER ModalSandboxMiddleware so sandbox_id is available in state.

    Flow:
    1. Fetch all threads via LangGraph API (Python HTTP)
    2. Filter for active threads (is_done != true)
    3. Dump each active thread to /workspace/threads/{thread_id}.txt
    4. Store active_thread_count in state
    """

    state_schema = TriageThreadsState

    def __init__(self, api_url: str | None = None):
        super().__init__()
        self._api_url = api_url or LANGGRAPH_API_URL

    def before_agent(
        self, state: TriageThreadsState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Fetch threads and dump active ones to sandbox."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            print("Warning: No modal_sandbox_id in state, cannot dump threads")
            return {"active_thread_count": 0}

        try:
            # Fetch all threads via LangGraph API
            threads = self._fetch_threads()

            # Filter for active task_agent threads only
            active_threads = [
                t for t in threads
                if t.get("metadata", {}).get("graph_id") == "task_agent"
                and not t.get("values", {}).get("is_done", False)
            ]

            # Dump active threads to sandbox
            sandbox = modal.Sandbox.from_id(sandbox_id)
            self._dump_threads_to_sandbox(sandbox, active_threads)

            return {"active_thread_count": len(active_threads)}

        except Exception as e:
            print(f"Warning: Could not fetch/dump threads: {e}")
            return {"active_thread_count": 0}

    def _fetch_threads(self) -> list[dict]:
        """Fetch all threads from LangGraph API."""
        url = f"{self._api_url}/threads/search"
        print(f"DEBUG: Fetching threads from {url}")
        response = httpx.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"limit": 100},
            timeout=30,
        )
        response.raise_for_status()
        threads = response.json()
        print(f"DEBUG: Fetched {len(threads)} threads")
        return threads

    def _dump_threads_to_sandbox(
        self, sandbox: modal.Sandbox, threads: list[dict]
    ) -> None:
        """Dump thread files to sandbox /workspace/threads/ in a single exec."""
        if not threads:
            # Just create empty directory
            process = sandbox.exec("mkdir", "-p", "/workspace/threads", timeout=10)
            process.wait()
            return

        # Build a single bash script that writes all files
        script_parts = ["mkdir -p /workspace/threads"]

        for thread in threads:
            thread_id = thread.get("thread_id", "unknown")
            thread_title = thread.get("values", {}).get("thread_title", "Untitled")

            # Format messages
            messages = thread.get("values", {}).get("messages", [])
            messages_text = "\n".join(
                f"[{m.get('type', 'unknown')}] {m.get('content', '')[:500]}"
                for m in messages
            )

            # Build file content
            content = f"""THREAD_ID: {thread_id}
TITLE: {thread_title}
---MESSAGES---
{messages_text}
"""
            # Use unique heredoc delimiter per file to avoid conflicts
            delimiter = f"EOF_{thread_id.replace('-', '_')}"
            script_parts.append(
                f"cat > '/workspace/threads/{thread_id}.txt' << '{delimiter}'\n{content}\n{delimiter}"
            )

        # Join all parts and execute as single script
        script = "\n".join(script_parts)
        process = sandbox.exec("bash", "-c", script, timeout=60)
        process.wait()

    async def abefore_agent(
        self, state: TriageThreadsState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version with non-blocking HTTP."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            print("Warning: No modal_sandbox_id in state, cannot dump threads")
            return {"active_thread_count": 0}

        try:
            # Fetch threads via async HTTP
            threads = await self._afetch_threads()

            # Filter for active task_agent threads only
            active_threads = [
                t for t in threads
                if t.get("metadata", {}).get("graph_id") == "task_agent"
                and not t.get("values", {}).get("is_done", False)
            ]

            # Dump active threads to sandbox
            sandbox = modal.Sandbox.from_id(sandbox_id)
            self._dump_threads_to_sandbox(sandbox, active_threads)

            return {"active_thread_count": len(active_threads)}

        except Exception as e:
            print(f"Warning: Could not fetch/dump threads: {e}")
            return {"active_thread_count": 0}

    async def _afetch_threads(self) -> list[dict]:
        """Fetch all threads from LangGraph API (async)."""
        url = f"{self._api_url}/threads/search"
        print(f"DEBUG: Fetching threads from {url} (async)")
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={"Content-Type": "application/json"},
                json={"limit": 100},
                timeout=30,
            )
        response.raise_for_status()
        threads = response.json()
        print(f"DEBUG: Fetched {len(threads)} threads")
        return threads

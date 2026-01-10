"""Triage rules middleware for reading triage rules from Modal volume."""

from __future__ import annotations

from typing import Any, NotRequired

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langgraph.runtime import Runtime


class TriageRulesState(AgentState):
    """State schema for triage rules middleware."""
    modal_sandbox_id: NotRequired[str]
    triage_rules: NotRequired[str]


class TriageRulesMiddleware(AgentMiddleware[TriageRulesState, Any]):
    """Middleware that reads triage rules from /memories/triage.md in Modal sandbox.

    Must run AFTER ModalSandboxMiddleware so sandbox_id is available in state.
    """

    state_schema = TriageRulesState

    def __init__(self, rules_path: str = "/memories/triage.md"):
        super().__init__()
        self._rules_path = rules_path

    def before_agent(
        self, state: TriageRulesState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Read triage rules from Modal sandbox volume."""
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            print("Warning: No modal_sandbox_id in state, cannot read triage rules")
            return None

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            process = sandbox.exec("cat", self._rules_path, timeout=10)
            process.wait()

            if process.returncode == 0:
                triage_rules = process.stdout.read()
                return {"triage_rules": triage_rules}
            else:
                stderr = process.stderr.read()
                print(f"Warning: Failed to read triage rules: {stderr}")
                return None
        except Exception as e:
            print(f"Warning: Could not read triage rules: {e}")
            return None

    async def abefore_agent(
        self, state: TriageRulesState, runtime: Runtime
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

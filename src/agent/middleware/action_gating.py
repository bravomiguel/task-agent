"""Action gating middleware — dynamic HITL approval for write/destructive actions.

Inspects AI tool calls after model output, classifies commands by service
and risk level, and interrupts for user approval when the service is gated.

Gating applies to:
- `execute` calls running CLI commands that write to external services
- `execute` calls running curl/wget with write HTTP methods
- `execute` calls running agent-browser destructive actions
- `send_message` with via="connection" (sending as user)

Gating does NOT apply to:
- Read-only CLI commands (list, view, get, search, status)
- Internal agent operations (manage_config, manage_crons, file ops, etc.)
- Cron/heartbeat sessions (no human present)
- send_message via chat_surface (agent speaking as itself)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain.agents.middleware.human_in_the_loop import (
    ActionRequest,
    ReviewConfig,
    HITLRequest,
    Decision,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command classifier — maps CLI commands to services and read/write
# ---------------------------------------------------------------------------

# Service detection patterns: (regex for command prefix) → service name
SERVICE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bgog\b"), "google"),
    (re.compile(r"\bgh\b"), "github"),
    (re.compile(r"\bnotion\b"), "notion"),
    (re.compile(r"\btrello\b"), "trello"),
    (re.compile(r"\bagent-browser\b"), "browser"),
]

# Write patterns per service — if any match, the command is destructive
WRITE_PATTERNS: dict[str, re.Pattern] = {
    "google": re.compile(
        r"\b(send|compose|create|update|delete|trash|modify|move|insert|patch|remove|star|unstar|label|archive)\b",
        re.IGNORECASE,
    ),
    "github": re.compile(
        r"\b(create|close|merge|delete|comment|edit|approve|review|label|assign|request|enable|disable|push|release|fork)\b",
        re.IGNORECASE,
    ),
    "notion": re.compile(
        r"\b(create|update|delete|archive|append|add|remove|move)\b",
        re.IGNORECASE,
    ),
    "trello": re.compile(
        r"\b(create|move|archive|delete|comment|assign|add|remove|update|close)\b",
        re.IGNORECASE,
    ),
    "browser": re.compile(
        r"\b(click|dblclick|fill|type|select|check|uncheck|upload|download|eval|tap|press|submit)\b",
        re.IGNORECASE,
    ),
}

# curl/wget write detection
CURL_WRITE_PATTERN = re.compile(
    r"\bcurl\b.*(-X\s*(POST|PUT|PATCH|DELETE)|-d\s|--data\b|--data-raw\b|--data-binary\b|--json\b)",
    re.IGNORECASE,
)
WGET_POST_PATTERN = re.compile(
    r"\bwget\b.*--post-(data|file)\b",
    re.IGNORECASE,
)


def classify_execute_command(command: str) -> tuple[str | None, bool]:
    """Classify an execute command: (service_name, is_write).

    Returns (None, False) if no external service detected.
    Returns (service, False) for read operations.
    Returns (service, True) for write/destructive operations.
    """
    # Check curl/wget first (any external service)
    if CURL_WRITE_PATTERN.search(command):
        return ("http", True)
    if WGET_POST_PATTERN.search(command):
        return ("http", True)

    # Check each service
    for pattern, service in SERVICE_PATTERNS:
        if pattern.search(command):
            write_pat = WRITE_PATTERNS.get(service)
            if write_pat and write_pat.search(command):
                return (service, True)
            return (service, False)

    return (None, False)


def is_service_gated(service: str, config: Any) -> bool:
    """Check if a service has action gating enabled."""
    if not config.action_gating.enabled:
        return False

    # curl/wget → gate if ANY service is gated (can't know which service the URL targets)
    if service == "http":
        return True

    return getattr(config.action_gating.services, service, False)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class ActionGatingMiddleware(AgentMiddleware):
    """Dynamic action gating via after_model interrupt.

    Inspects tool calls, classifies commands, and interrupts for approval
    when a gated external service is targeted with a write operation.
    """

    def _load_config(self, state: dict) -> Any | None:
        """Load action gating config."""
        try:
            from agent.config import load_config
            sandbox_id = state.get("modal_sandbox_id")
            if not sandbox_id:
                return None
            return load_config(sandbox_id)
        except Exception:
            return None

    def _classify_tool_call(
        self, tool_call: dict, config: Any
    ) -> bool:
        """Returns True if this tool call should be gated (needs approval)."""
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})

        # execute: classify the command
        if name == "execute":
            command = args.get("command", "")
            if not command:
                return False
            # Handle chained commands (&&, ;, |)
            # If any sub-command is a gated write, gate the whole thing
            for sub_cmd in re.split(r"[;&|]+", command):
                sub_cmd = sub_cmd.strip()
                if not sub_cmd:
                    continue
                service, is_write = classify_execute_command(sub_cmd)
                if service and is_write and is_service_gated(service, config):
                    return True
            return False

        # send_message with via="connection": gate if platform service is gated
        if name == "send_message":
            via = args.get("via", "")
            if via == "connection":
                platform = args.get("platform", "")
                service = "slack" if platform == "slack" else "teams"
                return is_service_gated(service, config)
            return False

        return False

    def after_model(self, state: dict, runtime: Runtime) -> dict[str, Any] | None:
        """Inspect tool calls and interrupt for gated write actions."""
        config = self._load_config(state)
        if not config or not config.action_gating.enabled:
            return None

        messages = state.get("messages", [])
        if not messages:
            return None

        last_ai_msg = next(
            (msg for msg in reversed(messages) if isinstance(msg, AIMessage)),
            None,
        )
        if not last_ai_msg or not last_ai_msg.tool_calls:
            return None

        # Classify each tool call
        action_requests: list[ActionRequest] = []
        review_configs: list[ReviewConfig] = []
        interrupt_indices: list[int] = []

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if self._classify_tool_call(tool_call, config):
                name = tool_call["name"]
                args = tool_call["args"]

                # Build a readable description
                if name == "execute":
                    desc = f"Shell command:\n{args.get('command', '')}"
                elif name == "send_message":
                    desc = (
                        f"Send message as user on {args.get('platform', '?')} "
                        f"to {args.get('recipient', '?')}:\n{args.get('text', '')[:500]}"
                    )
                else:
                    desc = f"Tool: {name}\nArgs: {json.dumps(args, indent=2)}"

                action_requests.append(
                    ActionRequest(name=name, args=args, description=desc)
                )
                review_configs.append(
                    ReviewConfig(
                        action_name=name,
                        allowed_decisions=["approve", "reject"],
                    )
                )
                interrupt_indices.append(idx)

        if not action_requests:
            return None

        # Interrupt for approval
        hitl_request = HITLRequest(
            action_requests=action_requests,
            review_configs=review_configs,
        )
        decisions: list[Decision] = interrupt(hitl_request)["decisions"]

        if len(decisions) != len(interrupt_indices):
            raise ValueError(
                f"Decisions count ({len(decisions)}) != interrupt count ({len(interrupt_indices)})"
            )

        # Process decisions
        revised_tool_calls = []
        rejection_messages: list[ToolMessage] = []
        decision_idx = 0

        for idx, tool_call in enumerate(last_ai_msg.tool_calls):
            if idx in interrupt_indices:
                decision = decisions[decision_idx]
                decision_idx += 1

                if decision["type"] == "approve":
                    revised_tool_calls.append(tool_call)
                elif decision["type"] == "reject":
                    content = decision.get("message") or (
                        f"User rejected {tool_call['name']}: {tool_call['args'].get('command', tool_call['args'])}"
                    )
                    rejection_messages.append(
                        ToolMessage(
                            content=content,
                            name=tool_call["name"],
                            tool_call_id=tool_call["id"],
                            status="error",
                        )
                    )
            else:
                revised_tool_calls.append(tool_call)

        last_ai_msg.tool_calls = revised_tool_calls
        return {"messages": [last_ai_msg, *rejection_messages]}

    async def aafter_model(self, state: dict, runtime: Runtime) -> dict[str, Any] | None:
        """Async version delegates to sync."""
        return self.after_model(state, runtime)

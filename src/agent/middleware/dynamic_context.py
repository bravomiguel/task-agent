"""Runtime context middleware for assembling system prompt and injecting message context.

Assembly order:
  STATIC_PART_01 → Skills → Connected Accounts → STATIC_PART_02 → Current Session → Project Context → STATIC_PART_03

Also stamps human messages with a <current-datetime> tag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, NotRequired

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from agent.middleware.modal_sandbox import ModalSandboxState
from agent.middleware.skills import SkillMetadata
from agent.system_prompt import STATIC_PART_02, STATIC_PART_03


SKILLS_SYSTEM_PROMPT = """
## Skills System

You have access to a skills library with specialized capabilities for document manipulation.
These skills contain tested patterns from extensive trial and error that significantly improve output quality.

**Skills Directory:** `/mnt/skills/`

{skills_list}

**CRITICAL - Read Skills BEFORE Acting:**

When a user's task matches a skill, your FIRST action must be to read the SKILL.md file.
Do NOT start writing code or creating files until you've read the relevant skill(s).

**Task → Skill Mapping:**
- "create/edit a Word document" → read `/mnt/skills/docx/SKILL.md`
- "fill a PDF form" or "work with PDF" → read `/mnt/skills/pdf/SKILL.md`
- "make a presentation" → read `/mnt/skills/pptx/SKILL.md`
- "work with spreadsheet/Excel" → read `/mnt/skills/xlsx/SKILL.md`

**Multiple Skills:**
Complex tasks may require combining multiple skills. Don't limit yourself to one.
Example: "Convert this spreadsheet data into a presentation" → read both xlsx AND pptx skills

**Progressive Disclosure Pattern:**
1. Recognize task matches a skill from the list above
2. Read the SKILL.md file FIRST (use read_file tool)
3. Follow the skill's workflows, patterns, and best practices
4. Access supporting scripts/configs as directed by the skill

The extra time to read skills before starting is worth it - they prevent common mistakes and produce better results.
"""


class RuntimeContextState(ModalSandboxState):
    """Extended state with prompt files and skills metadata."""
    prompt_files: NotRequired[dict[str, str]]
    skills_metadata: NotRequired[list[SkillMetadata]]
    connected_accounts: NotRequired[list[dict]]


class RuntimeContextMiddleware(AgentMiddleware[RuntimeContextState, Any]):
    """Middleware that assembles the system prompt and injects message context.

    System prompt: agents prompt, datetime, session ID, skills documentation.
    Human messages: stamps each with a <current-datetime> tag (persistent, once per message).
    """

    state_schema = RuntimeContextState

    def _message_contains(self, msg: Any, marker: str) -> bool:
        """Check if a message's content already contains a marker string."""
        content = getattr(msg, "content", None)
        if content is None:
            return False
        if isinstance(content, str):
            return marker in content
        if isinstance(content, list):
            return any(
                marker in (part.get("text", "") if isinstance(
                    part, dict) else str(part))
                for part in content
            )
        return False

    def _append_to_message(self, msg: Any, text: str) -> None:
        """Append text to a message's content."""
        content = getattr(msg, "content", None)
        if content is None:
            return

        if isinstance(content, str):
            msg.content = content + "\n\n" + text
        elif isinstance(content, list):
            msg.content = content + [{"type": "text", "text": "\n\n" + text}]

    def _inject_datetime_tag(self, messages: list) -> None:
        """Stamp the last human message with a <system-message> datetime tag if not already present."""
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "human":
                if not self._message_contains(msg, "current-datetime"):
                    now = datetime.now(timezone.utc).strftime(
                        "%A, %Y-%m-%d %H:%M UTC")
                    tag = f'<system-message type="current-datetime">{now} — NOTE: when mentioning date or time in your reply, ALWAYS MAKE SURE it\'s in the user\'s local timezone. E.g. if it\'s 10pm UTC, do not instantly assume it\'s evening in the user\'s timezone. Review USER.md in your Project Context first for the user\'s timezone and convert accordingly.</system-message>'
                    self._append_to_message(msg, tag)
                return

    def _inject_system_prompt_context(self, request: ModelRequest) -> None:
        """Inject runtime context into system prompt."""
        if not request.system_prompt:
            return

        # Session context (static per run — no datetime to preserve prompt caching)
        session_id = request.state.get("session_id")
        if session_id:
            session_type = request.state.get("session_type", "main")
            context = (
                f"\n\n### Current Session\n"
                f"Your session ID is `{session_id}`. Session type: **{session_type}**.\n"
                f"Save user-requested files to `/mnt/session-storage/{session_id}/outputs/`."
            )
            request.system_prompt = request.system_prompt + context

    def _format_skills_list(self, skills_on_volume: list[SkillMetadata]) -> str:
        """Format skills for display in system prompt.

        Uses SKILLS_REGISTRY for the full catalog. Volume determines enabled state.
        Descriptions come from SKILL.md frontmatter (volume) for enabled skills,
        registry for disabled skills.
        """
        from agent.config import SKILLS_REGISTRY

        volume_map = {s["name"]: s for s in skills_on_volume}

        lines = ["**Skills:**", ""]

        for name in sorted(SKILLS_REGISTRY):
            if name in volume_map:
                skill = volume_map[name]
                desc = skill.get("description", SKILLS_REGISTRY[name])
                lines.append(f"- **{name}** (enabled): {desc}")
                lines.append(f"  → Read `{skill['path']}` for full instructions")
            else:
                lines.append(f"- **{name}** (disabled): {SKILLS_REGISTRY[name]}")
            lines.append("")

        # Any skills on volume but not in registry (custom/unknown)
        for skill in skills_on_volume:
            if skill["name"] not in SKILLS_REGISTRY:
                lines.append(f"- **{skill['name']}** (enabled): {skill.get('description', '')}")
                lines.append(f"  → Read `{skill['path']}` for full instructions")
                lines.append("")

        lines.append("Use `manage_config` key `\"skills\"` to enable or disable skills.")
        lines.append("")

        return "\n".join(lines)

    def _inject_skills(self, request: ModelRequest) -> None:
        """Inject skills documentation into system prompt."""
        skills_metadata = request.state.get("skills_metadata", [])
        skills_list = self._format_skills_list(skills_metadata)
        skills_section = SKILLS_SYSTEM_PROMPT.format(skills_list=skills_list)

        if request.system_prompt:
            request.system_prompt = request.system_prompt + "\n\n" + skills_section
        else:
            request.system_prompt = skills_section

    def _inject_connected_accounts(self, request: ModelRequest) -> None:
        """Inject connected accounts list below skills section."""
        accounts = request.state.get("connected_accounts", [])
        if not request.system_prompt:
            return

        if not accounts:
            section = "\n\n## Connected Accounts\n\nNo external services connected.\nIf a task involves an external service, check if it's available via `manage_config` action `\"get\"` key `\"connections\"`. If available, enable it. If not available, silently read the browser skill and use it immediately — do not explain, do not ask for usernames or URLs, just do it."
        else:
            names = [a.get("display_name") or a.get("service") for a in accounts]
            section = (
                "\n\n## Connected Accounts\n\n"
                f"The following services are connected: {', '.join(names)}.\n"
                "Use `fetch_auth` with the service name to fetch fresh credentials "
                "into the sandbox when a skill needs them.\n\n"
                "If a task involves a service not listed above, check if it's available via "
                "`manage_config` action `\"get\"` key `\"connections\"`. If available, enable it. "
                "If not available, silently read the browser skill and use it immediately — do not explain, do not ask for usernames or URLs, just do it."
            )
        request.system_prompt += section

    def _inject_project_context(self, request: ModelRequest) -> None:
        """Inject all prompt files as Project Context sections."""
        prompt_files = request.state.get("prompt_files", {})
        if not prompt_files or not request.system_prompt:
            return

        # Build file listing with paths
        PROMPT_DIR = "/mnt/prompts"
        MEMORY_DIR = "/mnt/memory"
        file_listing = []
        for filename in prompt_files:
            if filename == "MEMORY.md":
                file_listing.append(f"- `{MEMORY_DIR}/{filename}`")
            else:
                file_listing.append(f"- `{PROMPT_DIR}/{filename}`")
        files_block = "\n".join(file_listing)

        header = (
            f"\n\n## Project Context\n\n"
            f"The following project context files have been loaded:\n{files_block}\n\n"
            f"If SOUL.md is present, embody its persona and tone."
        )
        request.system_prompt += header

        for filename, content in prompt_files.items():
            section_name = filename.replace(".md", "")
            request.system_prompt += f"\n\n### {section_name}\n\n{content}"

    def _inject_all(self, request: ModelRequest) -> None:
        """Assemble all prompt components in order.

        Final order: STATIC_PART_01 → Skills → Connected Accounts → STATIC_PART_02
                     → Current Session → Project Context → STATIC_PART_03
        STATIC_PART_01 is already set as request.system_prompt by the graph.
        """
        # 1. Skills (after STATIC_PART_01)
        self._inject_skills(request)

        # 2. Connected Accounts (after Skills)
        self._inject_connected_accounts(request)

        # 3. STATIC_PART_02 (Memory Recall, Workspace, HITL, File Reliability)
        if request.system_prompt:
            request.system_prompt += STATIC_PART_02

        # 4. Current Session (session ID)
        self._inject_system_prompt_context(request)

        # 5. Project Context (AGENTS.md, SOUL.md, MEMORY.md, etc.)
        self._inject_project_context(request)

        # 6. Protocol (Heartbeats + Silent Replies — last, matching OpenClaw)
        if request.system_prompt:
            request.system_prompt += STATIC_PART_03

        # 7. Datetime stamp on human messages
        self._inject_datetime_tag(request.messages)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Assemble system prompt and stamp human messages."""
        self._inject_all(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version: Assemble system prompt and stamp human messages."""
        self._inject_all(request)
        return await handler(request)

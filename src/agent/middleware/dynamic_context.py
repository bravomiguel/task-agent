"""Runtime context middleware for assembling system prompt and injecting message context.

Combines three concerns into a single wrap_model_call:
1. Agents prompt — appends AGENTS.md content to system prompt
2. Runtime context — appends datetime, session ID; stamps human messages
3. Skills — appends skills documentation to system prompt
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, NotRequired

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse

from agent.middleware.modal_sandbox import ModalSandboxState
from agent.middleware.skills import SkillMetadata


SKILLS_SYSTEM_PROMPT = """
## Skills System

You have access to a skills library with specialized capabilities for document manipulation.
These skills contain tested patterns from extensive trial and error that significantly improve output quality.

**Skills Directory:** `/default-user/skills/`

{skills_list}

**CRITICAL - Read Skills BEFORE Acting:**

When a user's task matches a skill, your FIRST action must be to read the SKILL.md file.
Do NOT start writing code or creating files until you've read the relevant skill(s).

**Task → Skill Mapping:**
- "create/edit a Word document" → read `/default-user/skills/docx/SKILL.md`
- "fill a PDF form" or "work with PDF" → read `/default-user/skills/pdf/SKILL.md`
- "make a presentation" → read `/default-user/skills/pptx/SKILL.md`
- "work with spreadsheet/Excel" → read `/default-user/skills/xlsx/SKILL.md`

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
                marker in (part.get("text", "") if isinstance(part, dict) else str(part))
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
        """Stamp the last human message with a <system-reminder> datetime tag if not already present."""
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "human":
                if not self._message_contains(msg, "current-datetime"):
                    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                    tag = f'<system-reminder type="current-datetime">{now}</system-reminder>'
                    self._append_to_message(msg, tag)
                return

    def _inject_system_prompt_context(self, request: ModelRequest) -> None:
        """Inject runtime context into system prompt."""
        if not request.system_prompt:
            return

        context = ""

        # Current date/time
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        context += f"\n\n### Current Date & Time\n{now}"

        # Session context
        session_id = request.state.get("session_id")
        if session_id:
            context += (
                f"\n\n### Current Session\n"
                f"Your session ID is `{session_id}`. "
                f"Save user-requested files to `/default-user/session-storage/{session_id}/outputs/`."
            )

        if context:
            request.system_prompt = request.system_prompt + context

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Format skills metadata for display in system prompt."""
        if not skills:
            return "(No skills available yet. Skills will appear in /default-user/skills/ when added.)"

        lines = ["**Available Skills:**", ""]

        for skill in skills:
            lines.append(f"- **{skill['name']}**: {skill['description']}")
            lines.append(f"  → Read `{skill['path']}` for full instructions")
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

    def _inject_project_context(self, request: ModelRequest) -> None:
        """Inject all prompt files as Project Context sections."""
        prompt_files = request.state.get("prompt_files", {})
        if not prompt_files or not request.system_prompt:
            return

        # Build file listing with paths
        PROMPT_DIR = "/default-user/prompts"
        MEMORY_DIR = "/default-user/memory"
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
            f"These are live files on disk. To edit them, use `edit_file` with the paths above. "
            f"To delete a file, use `execute_bash` with `rm <path>`.\n\n"
            f"If SOUL.md is present, embody its persona and tone."
        )
        request.system_prompt += header

        for filename, content in prompt_files.items():
            section_name = filename.replace(".md", "")
            request.system_prompt += f"\n\n### {section_name}\n\n{content}"

    def _inject_all(self, request: ModelRequest) -> None:
        """Assemble all prompt components in order."""
        # 1. Project context files (AGENTS.md, HEARTBEAT.md, SOUL.md, MEMORY.md, etc.)
        self._inject_project_context(request)

        # 2. Runtime context (datetime + session ID)
        self._inject_system_prompt_context(request)

        # 3. Datetime stamp on human messages
        self._inject_datetime_tag(request.messages)

        # 4. Skills documentation
        self._inject_skills(request)

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

"""Skills middleware for loading skills baked into Modal image.

Implements Anthropic's progressive disclosure pattern for agent skills:
1. Load skill metadata (name + description) from /skills directory at session start
2. Inject skills list into system prompt
3. Agent reads full SKILL.md on-demand when relevant

Skills are baked into the Modal image at build time from agent/skills/ directory.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Awaitable, NotRequired, TypedDict

import modal
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware import ModelRequest, ModelResponse


# Maximum size for SKILL.md files (10MB)
MAX_SKILL_FILE_SIZE = 10 * 1024 * 1024


class SkillMetadata(TypedDict):
    """Metadata for a skill."""
    name: str
    description: str
    path: str


class SkillsState(AgentState):
    """Extended state schema with skills metadata."""
    skills_metadata: NotRequired[list[SkillMetadata]]


SKILLS_SYSTEM_PROMPT = """
## Skills System

You have access to a skills library with specialized capabilities for document manipulation.
These skills contain tested patterns from extensive trial and error that significantly improve output quality.

**Skills Directory:** `/skills/`

{skills_list}

**CRITICAL - Read Skills BEFORE Acting:**

When a user's task matches a skill, your FIRST action must be to read the SKILL.md file.
Do NOT start writing code or creating files until you've read the relevant skill(s).

**Task → Skill Mapping:**
- "create/edit a Word document" → read `/skills/docx/SKILL.md`
- "fill a PDF form" or "work with PDF" → read `/skills/pdf/SKILL.md`
- "make a presentation" → read `/skills/pptx/SKILL.md`
- "work with spreadsheet/Excel" → read `/skills/xlsx/SKILL.md`
- "search threads" or "create/manage runs" → read `/skills/langgraph-api/SKILL.md`
- "triage incoming email/message/webhook" → read `/skills/task-intake/SKILL.md`

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


def _parse_skill_metadata(skill_md_path: Path, sandbox: modal.Sandbox) -> SkillMetadata | None:
    """Parse YAML frontmatter from a SKILL.md file in the sandbox.

    Args:
        skill_md_path: Path to SKILL.md in the sandbox
        sandbox: Modal sandbox to read from

    Returns:
        SkillMetadata if valid, None if parsing fails
    """
    try:
        # Read file from sandbox
        process = sandbox.exec("cat", str(skill_md_path), timeout=10)
        process.wait()
        if process.returncode != 0:
            return None
        content = process.stdout.read()

        # Check file size
        if len(content) > MAX_SKILL_FILE_SIZE:
            return None

        # Parse YAML frontmatter
        frontmatter_pattern = r"^---\s*\n(.*?)\n---\s*\n"
        match = re.match(frontmatter_pattern, content, re.DOTALL)
        if not match:
            return None

        frontmatter = match.group(1)

        # Parse key-value pairs (simple parsing, no nested structures)
        metadata: dict[str, str] = {}
        for line in frontmatter.split("\n"):
            kv_match = re.match(r"^(\w+):\s*(.+)$", line.strip())
            if kv_match:
                key, value = kv_match.groups()
                metadata[key] = value.strip()

        # Validate required fields
        if "name" not in metadata or "description" not in metadata:
            return None

        return SkillMetadata(
            name=metadata["name"],
            description=metadata["description"],
            path=str(skill_md_path),
        )
    except Exception:
        return None


def _list_skills_from_sandbox(sandbox: modal.Sandbox, skills_dir: str = "/skills") -> list[SkillMetadata]:
    """List all skills from the sandbox's /skills directory.

    Args:
        sandbox: Modal sandbox with skills baked into image
        skills_dir: Path to skills directory in sandbox

    Returns:
        List of skill metadata
    """
    skills: list[SkillMetadata] = []

    try:
        # List skill directories
        process = sandbox.exec("ls", "-1", skills_dir, timeout=10)
        process.wait()
        if process.returncode != 0:
            return []

        skill_names = process.stdout.read().strip().split("\n")

        for skill_name in skill_names:
            if not skill_name:
                continue

            skill_md_path = Path(skills_dir) / skill_name / "SKILL.md"

            # Check if SKILL.md exists
            check_process = sandbox.exec("test", "-f", str(skill_md_path), timeout=5)
            check_process.wait()
            if check_process.returncode != 0:
                continue

            # Parse metadata
            metadata = _parse_skill_metadata(skill_md_path, sandbox)
            if metadata:
                skills.append(metadata)

    except Exception:
        pass

    return skills


class SkillsMiddleware(AgentMiddleware[SkillsState, Any]):
    """Middleware for loading and exposing agent skills from Modal image.

    Implements progressive disclosure:
    1. Parse YAML frontmatter from SKILL.md files at session start
    2. Inject skills metadata (name + description) into system prompt
    3. Agent reads full SKILL.md content when relevant to a task

    Skills are baked into the Modal image at /skills/ directory.
    """

    state_schema = SkillsState

    def __init__(
        self,
        skills_path: str = "/skills",
    ) -> None:
        """Initialize the skills middleware.

        Args:
            skills_path: Path to skills directory in sandbox (baked into image)
        """
        super().__init__()
        self.skills_path = skills_path

    def before_agent(
        self, state: SkillsState, runtime: Any
    ) -> dict[str, Any] | None:
        """Load skills metadata from the /skills directory.

        Discovers available skills from the /skills directory at the start
        of each interaction.
        """
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return None

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)

            # Load skills metadata
            skills = _list_skills_from_sandbox(sandbox, self.skills_path)

            return {"skills_metadata": skills}
        except Exception:
            return None

    async def abefore_agent(
        self, state: SkillsState, runtime: Any
    ) -> dict[str, Any] | None:
        """Async version delegates to sync implementation."""
        return self.before_agent(state, runtime)

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """Format skills metadata for display in system prompt."""
        if not skills:
            return "(No skills available yet. Skills will appear in /skills/ when added.)"

        lines = ["**Available Skills:**", ""]

        for skill in skills:
            lines.append(f"- **{skill['name']}**: {skill['description']}")
            lines.append(f"  → Read `{skill['path']}` for full instructions")
            lines.append("")

        return "\n".join(lines)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Inject skills documentation into the system prompt."""
        skills_metadata = request.state.get("skills_metadata", [])

        # Format skills list
        skills_list = self._format_skills_list(skills_metadata)

        # Format the skills documentation
        skills_section = SKILLS_SYSTEM_PROMPT.format(skills_list=skills_list)

        if request.system_prompt:
            request.system_prompt = request.system_prompt + "\n\n" + skills_section
        else:
            request.system_prompt = skills_section

        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async version of wrap_model_call."""
        skills_metadata = request.state.get("skills_metadata", [])

        # Format skills list
        skills_list = self._format_skills_list(skills_metadata)

        # Format the skills documentation
        skills_section = SKILLS_SYSTEM_PROMPT.format(skills_list=skills_list)

        if request.system_prompt:
            request.system_prompt = request.system_prompt + "\n\n" + skills_section
        else:
            request.system_prompt = skills_section

        return await handler(request)

"""Skills middleware for Modal volume-based skill loading.

Implements Anthropic's progressive disclosure pattern for agent skills:
1. Load skill metadata (name + description) from /skills volume at session start
2. Inject skills list into system prompt
3. Agent reads full SKILL.md on-demand when relevant
4. Auto-seeds skills from local directory if not present in volume
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

# Local skills directory (relative to agent package)
LOCAL_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"


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

You have access to a skills library that provides specialized capabilities and domain knowledge.

**Skills Directory:** `/skills/`

{skills_list}

**How to Use Skills (Progressive Disclosure):**

Skills follow a **progressive disclosure** pattern:

1. **Recognize when a skill applies**: Check if the user's task matches any skill's description
2. **Read the skill's full instructions**: Use read_file or execute with cat to read the SKILL.md path shown
3. **Follow the skill's instructions**: SKILL.md contains step-by-step workflows, best practices, and examples
4. **Access supporting files**: Skills may include Python scripts or configs in their directory

**Skills are Self-Documenting:**
- Each SKILL.md tells you exactly what the skill does and how to use it
- The skill list above shows the full path for each skill's SKILL.md file
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
        sandbox: Modal sandbox with skills volume mounted
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


def _seed_skills_to_volume(volume: modal.Volume, local_skills_dir: Path) -> None:
    """Seed skills from local directory to Modal volume.

    Args:
        volume: Modal volume to seed skills into
        local_skills_dir: Local directory containing skill folders
    """
    if not local_skills_dir.exists():
        return

    for skill_dir in local_skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        # Upload all files in skill directory
        for file_path in skill_dir.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(local_skills_dir)
                remote_path = f"/{relative_path}"

                with open(file_path, "rb") as f:
                    volume.write_file(remote_path, f)

    volume.commit()


def _check_and_seed_skills(sandbox: modal.Sandbox, volume: modal.Volume, local_skills_dir: Path) -> None:
    """Check if skills exist in volume, seed if missing.

    Args:
        sandbox: Modal sandbox to check skills in
        volume: Modal volume to seed skills into
        local_skills_dir: Local directory containing skill folders
    """
    if not local_skills_dir.exists():
        return

    # Get list of local skills
    local_skill_names = {
        d.name for d in local_skills_dir.iterdir()
        if d.is_dir() and (d / "SKILL.md").exists()
    }

    if not local_skill_names:
        return

    # Check which skills exist in volume
    try:
        process = sandbox.exec("ls", "-1", "/skills", timeout=10)
        process.wait()
        if process.returncode == 0:
            existing_skills = set(process.stdout.read().strip().split("\n"))
        else:
            existing_skills = set()
    except Exception:
        existing_skills = set()

    # Find missing skills
    missing_skills = local_skill_names - existing_skills

    if not missing_skills:
        return

    # Seed missing skills
    for skill_name in missing_skills:
        skill_dir = local_skills_dir / skill_name

        for file_path in skill_dir.rglob("*"):
            if file_path.is_file():
                relative_path = file_path.relative_to(local_skills_dir)
                remote_path = f"/{relative_path}"

                with open(file_path, "rb") as f:
                    volume.write_file(remote_path, f)

    volume.commit()

    # Reload volume in sandbox to see new files
    volume.reload()


class SkillsMiddleware(AgentMiddleware[SkillsState, Any]):
    """Middleware for loading and exposing agent skills from Modal volume.

    Implements progressive disclosure:
    1. Parse YAML frontmatter from SKILL.md files at session start
    2. Inject skills metadata (name + description) into system prompt
    3. Agent reads full SKILL.md content when relevant to a task
    4. Auto-seeds skills from local directory if not present in volume
    """

    state_schema = SkillsState

    def __init__(
        self,
        skills_volume_name: str = "skills",
        skills_mount_path: str = "/skills",
        local_skills_dir: Path | None = None,
    ) -> None:
        """Initialize the skills middleware.

        Args:
            skills_volume_name: Name of the Modal volume for skills
            skills_mount_path: Mount path in sandbox
            local_skills_dir: Local directory to seed skills from (defaults to agent/skills)
        """
        super().__init__()
        self.skills_volume_name = skills_volume_name
        self.skills_mount_path = skills_mount_path
        self.local_skills_dir = local_skills_dir or LOCAL_SKILLS_DIR
        self._skills_volume: modal.Volume | None = None

    def get_skills_volume(self) -> modal.Volume:
        """Get or create the skills volume."""
        if self._skills_volume is None:
            self._skills_volume = modal.Volume.from_name(
                self.skills_volume_name,
                create_if_missing=True,
                version=2
            )
        return self._skills_volume

    def before_agent(
        self, state: SkillsState, runtime: Any
    ) -> dict[str, Any] | None:
        """Load skills metadata and auto-seed if needed.

        Discovers available skills from the /skills volume at the start
        of each interaction. Seeds missing skills from local directory.
        """
        sandbox_id = state.get("modal_sandbox_id")
        if not sandbox_id:
            return None

        try:
            sandbox = modal.Sandbox.from_id(sandbox_id)
            volume = self.get_skills_volume()

            # Auto-seed missing skills
            _check_and_seed_skills(sandbox, volume, self.local_skills_dir)

            # Load skills metadata
            skills = _list_skills_from_sandbox(sandbox, self.skills_mount_path)

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

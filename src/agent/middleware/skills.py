"""Skills helpers for discovering skills from user volume.

Provides module-level functions for parsing skill metadata from SKILL.md files
in the sandbox. Used by SessionSetupMiddleware for skill discovery.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypedDict

import modal


# Maximum size for SKILL.md files (10MB)
MAX_SKILL_FILE_SIZE = 10 * 1024 * 1024


class SkillMetadata(TypedDict):
    """Metadata for a skill."""
    name: str
    description: str
    path: str


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


def _list_skills_from_sandbox(sandbox: modal.Sandbox, skills_dir: str = "/default-user/skills") -> list[SkillMetadata]:
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

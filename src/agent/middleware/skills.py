"""Skills helpers for discovering skills from user volume.

Provides module-level functions for parsing skill metadata from SKILL.md files
in the sandbox. Used by SessionSetupMiddleware for skill discovery.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TypedDict

import modal

logger = logging.getLogger(__name__)


class SkillMetadata(TypedDict):
    """Metadata for a skill."""
    name: str
    description: str
    path: str


# Single script that reads all SKILL.md frontmatter AND the manifest in one exec.
# Outputs skill blocks first, then the manifest JSON after a separator.
_SKILLS_SCRIPT = (
    'for d in {skills_dir}/*/; do '
    '  f="${{d}}SKILL.md"; '
    '  [ -f "$f" ] || continue; '
    '  echo "===SKILL_PATH:$f==="; '
    '  head -20 "$f"; '
    'done; '
    'echo "===MANIFEST==="; '
    'cat {skills_dir}/_manifest/manifest.json 2>/dev/null || echo "[]"'
)


def _parse_skills_output(output: str) -> tuple[list[SkillMetadata], list[dict[str, str]]]:
    """Parse combined output into skill metadata list and manifest.

    Returns:
        Tuple of (skills on volume, manifest entries).
    """
    # Split manifest from skill blocks
    manifest: list[dict[str, str]] = []
    skill_output = output
    if "===MANIFEST===" in output:
        skill_output, manifest_raw = output.split("===MANIFEST===", 1)
        manifest_raw = manifest_raw.strip()
        if manifest_raw:
            try:
                manifest = json.loads(manifest_raw)
            except json.JSONDecodeError as e:
                logger.warning("[Skills] manifest JSON parse error: %s", e)

    # Parse skill blocks
    skills: list[SkillMetadata] = []
    for block in skill_output.split("===SKILL_PATH:")[1:]:
        try:
            separator_end = block.index("===\n")
        except ValueError:
            continue
        skill_path = block[:separator_end]
        content = block[separator_end + 4:]

        frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not frontmatter_match:
            continue

        metadata: dict[str, str] = {}
        for line in frontmatter_match.group(1).split("\n"):
            kv_match = re.match(r"^(\w+):\s*(.+)$", line.strip())
            if kv_match:
                key, value = kv_match.groups()
                metadata[key] = value.strip()

        if "name" in metadata and "description" in metadata:
            skills.append(SkillMetadata(
                name=metadata["name"],
                description=metadata["description"],
                path=skill_path,
            ))
    return skills, manifest


def _list_skills_from_sandbox(
    sandbox: modal.Sandbox, skills_dir: str = "/mnt/skills",
) -> tuple[list[SkillMetadata], list[dict[str, str]]]:
    """List all skills from the sandbox's /skills directory.

    Uses a single sandbox.exec call to read all SKILL.md frontmatter and the
    manifest at once. Retries if the directory count exceeds parsed skills
    (volume still syncing).

    Args:
        sandbox: Modal sandbox with skills baked into image
        skills_dir: Path to skills directory in sandbox

    Returns:
        Tuple of (skills on volume, manifest entries).
    """
    try:
        # First, count how many skill dirs exist on the volume
        count_proc = sandbox.exec(
            "bash", "-c",
            f'ls -1d {skills_dir}/*/ 2>/dev/null | grep -v "/_manifest/" | wc -l',
            timeout=10,
        )
        count_proc.wait()
        expected = int(count_proc.stdout.read().strip() or "0")

        script = _SKILLS_SCRIPT.format(skills_dir=skills_dir)

        # Try up to 3 times, waiting for volume to fully sync
        manifest: list[dict[str, str]] = []
        for attempt in range(3):
            process = sandbox.exec("bash", "-c", script, timeout=15)
            process.wait()
            output = process.stdout.read()
            skills, manifest = _parse_skills_output(output)

            if len(skills) >= expected or expected == 0:
                logger.info("[Skills] loaded %d/%d skills, manifest=%d", len(skills), expected, len(manifest))
                return skills, manifest

            logger.info(
                "[Skills] found %d/%d skills (attempt %d), waiting for volume sync...",
                len(skills), expected, attempt + 1,
            )
            time.sleep(1)

        # Return whatever we got on final attempt
        logger.warning("[Skills] returning %d/%d skills after retries", len(skills), expected)
        return skills, manifest

    except Exception:
        return [], []

#!/usr/bin/env python3
"""Sync local skills directory to Modal Volume.

Run this script whenever you add or update skills:
    python sync_skills.py

Or with modal run:
    modal run sync_skills.py
"""

import modal
from pathlib import Path

SKILLS_DIR = Path(__file__).parent
VOLUME_NAME = "skills"

app = modal.App("skills-sync")
skills_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True, version=2)


@app.function(
    image=modal.Image.debian_slim(),
    volumes={"/skills": skills_volume},
    timeout=300,
)
def sync_skills(skills_data: dict[str, dict[str, bytes]]):
    """Sync skills data to the Modal Volume.

    Args:
        skills_data: Dict mapping skill_name -> {filename: content}
    """
    import os
    import shutil

    # Clear existing skills
    for item in Path("/skills").iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Write new skills
    for skill_name, files in skills_data.items():
        skill_dir = Path("/skills") / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)

        for filename, content in files.items():
            file_path = skill_dir / filename
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
            print(f"  Wrote: /skills/{skill_name}/{filename}")

    # Commit the volume
    skills_volume.commit()
    print(f"\nSynced {len(skills_data)} skills to volume '{VOLUME_NAME}'")


@app.local_entrypoint()
def main():
    """Read local skills and sync to Modal Volume."""
    if not SKILLS_DIR.exists():
        print(f"Skills directory not found: {SKILLS_DIR}")
        return

    # Collect all skills data
    skills_data: dict[str, dict[str, bytes]] = {}

    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_name = skill_dir.name
        skills_data[skill_name] = {}

        # Recursively collect all files in the skill directory
        for file_path in skill_dir.rglob("*"):
            # Skip __pycache__ directories
            if "__pycache__" in file_path.parts:
                continue

            if file_path.is_file():
                relative_path = file_path.relative_to(skill_dir)
                skills_data[skill_name][str(relative_path)] = file_path.read_bytes()

    if not skills_data:
        print("No skills found to sync")
        return

    print(f"Syncing {len(skills_data)} skills to Modal Volume '{VOLUME_NAME}':")
    for skill_name in sorted(skills_data.keys()):
        print(f"  - {skill_name} ({len(skills_data[skill_name])} files)")
    print()

    # Run the sync function
    sync_skills.remote(skills_data)

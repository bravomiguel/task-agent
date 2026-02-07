#!/usr/bin/env python3
"""Sync local prompts directory to Modal Volume.

Run this script whenever you add or update prompt files:
    python sync_prompts.py

Or with modal run:
    modal run sync_prompts.py
"""

import modal
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent
VOLUME_NAME = "user-default-user"

app = modal.App("prompts-sync")
user_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True, version=2)


@app.function(
    image=modal.Image.debian_slim(),
    volumes={"/default-user": user_volume},
    timeout=300,
)
def sync_prompts(prompts_data: dict[str, bytes]):
    """Sync prompts data to the Modal Volume.

    Args:
        prompts_data: Dict mapping filename -> content
    """
    import shutil

    # Ensure prompts directory exists
    prompts_path = Path("/default-user/prompts")
    prompts_path.mkdir(parents=True, exist_ok=True)

    # Clear existing prompts
    for item in prompts_path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Write new prompts
    for filename, content in prompts_data.items():
        file_path = prompts_path / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        print(f"  Wrote: /default-user/prompts/{filename}")

    # Commit the volume
    user_volume.commit()
    print(f"\nSynced {len(prompts_data)} files to volume '{VOLUME_NAME}'")


@app.local_entrypoint()
def main():
    """Read local prompts and sync to Modal Volume."""
    if not PROMPTS_DIR.exists():
        print(f"Prompts directory not found: {PROMPTS_DIR}")
        return

    # Collect all prompt files (excluding __pycache__ and .py files)
    prompts_data: dict[str, bytes] = {}

    for file_path in PROMPTS_DIR.rglob("*"):
        if "__pycache__" in file_path.parts:
            continue
        if file_path.suffix == ".py":
            continue

        if file_path.is_file():
            relative_path = file_path.relative_to(PROMPTS_DIR)
            prompts_data[str(relative_path)] = file_path.read_bytes()

    if not prompts_data:
        print("No prompt files found to sync")
        return

    print(f"Syncing {len(prompts_data)} files to Modal Volume '{VOLUME_NAME}':")
    for filename in sorted(prompts_data.keys()):
        print(f"  - {filename}")
    print()

    # Run the sync function
    sync_prompts.remote(prompts_data)

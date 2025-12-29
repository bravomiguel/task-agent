#!/usr/bin/env python3
"""Sync local memories directory to Modal Volume.

Run this script whenever you add or update memories:
    python sync_memories.py

Or with modal run:
    modal run sync_memories.py
"""

import modal
from pathlib import Path

MEMORIES_DIR = Path(__file__).parent
VOLUME_NAME = "memories"

app = modal.App("memories-sync")
memories_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True, version=2)


@app.function(
    image=modal.Image.debian_slim(),
    volumes={"/memories": memories_volume},
    timeout=300,
)
def sync_memories(memories_data: dict[str, bytes]):
    """Sync memories data to the Modal Volume.

    Args:
        memories_data: Dict mapping filename -> content
    """
    import os
    import shutil

    # Clear existing memories (but preserve runtime-created files by only removing synced ones)
    # For now, we do a full clear like skills - can make incremental later
    for item in Path("/memories").iterdir():
        if item.is_file() and item.suffix == ".md":
            item.unlink()

    # Write new memories
    for filename, content in memories_data.items():
        file_path = Path("/memories") / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        print(f"  Wrote: /memories/{filename}")

    # Commit the volume
    memories_volume.commit()
    print(f"\nSynced {len(memories_data)} memories to volume '{VOLUME_NAME}'")


@app.local_entrypoint()
def main():
    """Read local memories and sync to Modal Volume."""
    if not MEMORIES_DIR.exists():
        print(f"Memories directory not found: {MEMORIES_DIR}")
        return

    # Collect all memory files (only .md files at top level)
    memories_data: dict[str, bytes] = {}

    for file_path in MEMORIES_DIR.iterdir():
        # Skip non-files, hidden files, and non-markdown files
        if not file_path.is_file():
            continue
        if file_path.name.startswith("__") or file_path.name.startswith("."):
            continue
        if file_path.suffix != ".md":
            continue

        memories_data[file_path.name] = file_path.read_bytes()

    if not memories_data:
        print("No memories found to sync")
        return

    print(f"Syncing {len(memories_data)} memories to Modal Volume '{VOLUME_NAME}':")
    for name in sorted(memories_data.keys()):
        print(f"  - {name}")
    print()

    # Run the sync function
    sync_memories.remote(memories_data)

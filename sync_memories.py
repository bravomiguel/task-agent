#!/usr/bin/env python3
"""Sync local memories directory to Modal Volume.

Run this script whenever you add or update memory files:
    python sync_memories.py

Or with modal run:
    modal run sync_memories.py
"""

import modal
from pathlib import Path

MEMORIES_DIR = Path(__file__).parent / "memories"
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
    import shutil

    # Clear existing memories
    for item in Path("/memories").iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Write new memories
    for filename, content in memories_data.items():
        file_path = Path("/memories") / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        print(f"  Wrote: /memories/{filename}")

    # Commit the volume
    memories_volume.commit()
    print(f"\nSynced {len(memories_data)} files to volume '{VOLUME_NAME}'")


@app.local_entrypoint()
def main():
    """Read local memories and sync to Modal Volume."""
    if not MEMORIES_DIR.exists():
        print(f"Memories directory not found: {MEMORIES_DIR}")
        return

    # Collect all memory files (excluding __pycache__)
    memories_data: dict[str, bytes] = {}

    for file_path in MEMORIES_DIR.rglob("*"):
        # Skip __pycache__ directories
        if "__pycache__" in file_path.parts:
            continue

        if file_path.is_file():
            relative_path = file_path.relative_to(MEMORIES_DIR)
            memories_data[str(relative_path)] = file_path.read_bytes()

    if not memories_data:
        print("No memory files found to sync")
        return

    print(f"Syncing {len(memories_data)} files to Modal Volume '{VOLUME_NAME}':")
    for filename in sorted(memories_data.keys()):
        print(f"  - {filename}")
    print()

    # Run the sync function
    sync_memories.remote(memories_data)

"""Delete a subfolder (or file) from a Modal volume.

Usage:
    modal run scripts/delete_volume_path.py --path /session-storage
    modal run scripts/delete_volume_path.py --path /memory/.lancedb
    modal run scripts/delete_volume_path.py --path /session-transcripts --volume user-default-user
    modal run scripts/delete_volume_path.py --path /session-storage --dry-run
"""

import modal

app = modal.App("delete-volume-path")


@app.function(image=modal.Image.debian_slim())
def delete_path(volume_name: str, path: str, dry_run: bool = False):
    import subprocess

    volume = modal.Volume.from_name(volume_name, create_if_missing=False)

    # Create a temporary sandbox with the volume mounted so we can use rm -rf
    sb = modal.Sandbox.create(
        image=modal.Image.debian_slim(),
        volumes={"/vol": volume},
        timeout=120,
    )

    try:
        full_path = f"/vol{path}"

        # Show what we're about to delete
        proc = sb.exec("find", full_path, "-maxdepth", "1", timeout=10)
        proc.wait()
        listing = proc.stdout.read().strip()

        if proc.returncode != 0:
            print(f"Path not found: {path}")
            return

        lines = listing.split("\n") if listing else []
        print(f"Found {len(lines)} items at {path}:")
        for line in lines[:20]:
            print(f"  {line.replace('/vol', '')}")
        if len(lines) > 20:
            print(f"  ... and {len(lines) - 20} more")

        if dry_run:
            print("\n[DRY RUN] No files deleted.")
            return

        # Delete
        proc = sb.exec("rm", "-rf", full_path, timeout=60)
        proc.wait()

        if proc.returncode == 0:
            # Sync to persist deletion
            proc = sb.exec("sync", "/vol", timeout=30)
            proc.wait()
            print(f"\nDeleted {path}")
        else:
            stderr = proc.stderr.read()
            print(f"\nFailed to delete: {stderr}")

    finally:
        sb.terminate()


@app.local_entrypoint()
def main(
    path: str,
    volume: str = "user-default-user",
    dry_run: bool = False,
):
    delete_path.remote(volume, path, dry_run)

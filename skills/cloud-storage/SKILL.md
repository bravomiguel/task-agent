---
name: cloud-storage
description: Cloud storage file management for Dropbox and Box via rclone. Use this when you need to list, upload, download, copy, or sync files in the user's Dropbox or Box account.
---

# Cloud Storage (rclone)

Manage Dropbox and Box files via rclone CLI.

## Authentication

Before using rclone, set up the token exports for the target service:

### Dropbox

```bash
export RCLONE_CONFIG_DROPBOX_TYPE=dropbox
export RCLONE_CONFIG_DROPBOX_TOKEN='{"access_token":"'$(cat /workspace/.auth/dropbox_token)'","token_type":"bearer"}'
```

### Box

```bash
export RCLONE_CONFIG_BOX_TYPE=box
export RCLONE_CONFIG_BOX_TOKEN='{"access_token":"'$(cat /workspace/.auth/box_token)'","token_type":"bearer"}'
```

Run the exports before any rclone command.

## Install

rclone may not be pre-installed. Install if needed:

```bash
which rclone || curl -s https://rclone.org/install.sh | bash
```

## Commands

Remote name matches the env var prefix: `dropbox:` or `box:`.

### List files

```bash
# List files and sizes
rclone ls dropbox:

# List files in a folder
rclone ls dropbox:Documents/

# List top-level only (not recursive)
rclone lsf dropbox:

# JSON output (best for parsing)
rclone lsjson dropbox:Documents/ --no-modtime
```

### Download

Always download to the session workspace folder: `/mnt/session-storage/{session_id}/workspace/`

```bash
# Download a file
rclone copyto dropbox:Documents/report.pdf /mnt/session-storage/{session_id}/workspace/report.pdf

# Download a folder
rclone copy dropbox:Documents/project/ /mnt/session-storage/{session_id}/workspace/project/
```

**Never use `rclone cat`** — it dumps file contents to stdout and bloats context. Always download to disk first, then read from there.

### Upload

```bash
# Upload a file
rclone copyto /mnt/session-storage/{session_id}/workspace/report.pdf dropbox:Documents/report.pdf

# Upload a folder
rclone copy /mnt/session-storage/{session_id}/workspace/output/ dropbox:Documents/output/
```

### Copy between remotes

```bash
# Copy from Dropbox to Box
rclone copy dropbox:Documents/file.pdf box:Documents/file.pdf
```

### Sync

```bash
# Make remote folder match local (WARNING: deletes extra files at destination)
rclone sync /workspace/output/ dropbox:Backups/output/
```

### Other operations

```bash
# Create directory
rclone mkdir dropbox:NewFolder

# Delete file
rclone deletefile dropbox:Documents/old.pdf

# Delete empty directory
rclone rmdir dropbox:EmptyFolder

# Move file
rclone moveto dropbox:old/path.pdf dropbox:new/path.pdf

# Disk usage
rclone about dropbox:
```

## Tips

- Use `rclone copy` (additive) instead of `rclone sync` (destructive) unless you specifically need mirroring.
- Confirm with the user before deleting files or using `sync`.
- `lsjson` is best for programmatic use; `lsf` for human-readable lists.
- Both Dropbox and Box remotes work identically — just swap the remote name.
- Use `--progress` flag for large transfers to show progress.

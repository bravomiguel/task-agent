#!/usr/bin/env bash
# Factory reset: wipe entire volume and restore expected structure.
#
# Usage:
#   ./scripts/reset_volume.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MODAL="$PROJECT_DIR/.venv/bin/modal"

KEEPFILE=$(mktemp)
touch "$KEEPFILE"
trap "rm -f $KEEPFILE" EXIT

echo "=== Factory Reset ==="

# Step 1: Wipe everything on the volume
echo "Wiping entire volume..."
for item in $("$MODAL" volume ls user-default-user / 2>/dev/null); do
  if [ "$item" = "." ] || [ "$item" = ".." ]; then continue; fi
  "$MODAL" volume rm user-default-user "/$item" -r 2>/dev/null || \
  "$MODAL" volume rm user-default-user "/$item" 2>/dev/null || true
done

# Step 2: Recreate empty directory structure
echo "Creating directory structure..."
for dir in memory session-storage session-transcripts .temp-uploads; do
  "$MODAL" volume put user-default-user "$KEEPFILE" "/$dir/.keep" --force
done

# Step 3: Restore default config
echo "Restoring default config..."
"$MODAL" volume put user-default-user "$PROJECT_DIR/config.default.json" /config.json --force

# Step 4: Restore default prompts
echo "Restoring default prompts..."
"$SCRIPT_DIR/reset_prompts.sh"

# Step 5: Restore default skills
echo "Restoring default skills..."
"$SCRIPT_DIR/reset_skills.sh"

# Step 6: Reset heartbeat cron
echo "Resetting heartbeat cron..."
"$PROJECT_DIR/venv/bin/python" "$SCRIPT_DIR/reset_heartbeat_cron.py"

echo "=== Factory reset complete ==="

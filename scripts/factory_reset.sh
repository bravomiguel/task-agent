#!/usr/bin/env bash
# Factory reset: wipe LangGraph threads and Modal volume, restore expected structure.
#
# Usage:
#   ./scripts/factory_reset.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MODAL="$PROJECT_DIR/.venv/bin/modal"

KEEPFILE=$(mktemp)
touch "$KEEPFILE"
trap "rm -f $KEEPFILE" EXIT

echo "=== Factory Reset ==="

# Step 1: Wipe LangGraph threads (local dev storage)
echo "Wiping LangGraph threads..."
rm -rf "$PROJECT_DIR/.langgraph_api"

# Step 2: Wipe everything on the volume
echo "Wiping entire volume..."
for item in $("$MODAL" volume ls user-dev / 2>/dev/null); do
  if [ "$item" = "." ] || [ "$item" = ".." ]; then continue; fi
  "$MODAL" volume rm user-dev "/$item" -r 2>/dev/null || \
  "$MODAL" volume rm user-dev "/$item" 2>/dev/null || true
done

# Step 3: Recreate empty directory structure
echo "Creating directory structure..."
for dir in memory session-storage session-transcripts .temp-uploads; do
  "$MODAL" volume put user-dev "$KEEPFILE" "/$dir/.keep" --force
done

# Step 4: Restore default config
echo "Restoring default config..."
"$MODAL" volume put user-dev "$PROJECT_DIR/config.default.json" /config.json --force

# Step 5: Restore default prompts
echo "Restoring default prompts..."
"$SCRIPT_DIR/reset_prompts.sh"

# Step 6: Restore default skills
echo "Restoring default skills..."
"$SCRIPT_DIR/reset_skills.sh"

# Step 7: Reset heartbeat cron
echo "Resetting heartbeat cron..."
"$PROJECT_DIR/venv/bin/python" "$SCRIPT_DIR/reset_heartbeat_cron.py"

echo "=== Factory reset complete ==="

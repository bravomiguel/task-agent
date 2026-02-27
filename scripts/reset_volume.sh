#!/usr/bin/env bash
# Factory reset: wipe memory, sessions, and restore default prompts.
#
# Usage:
#   ./scripts/reset_volume.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MODAL="$PROJECT_DIR/.venv/bin/modal"

echo "=== Factory Reset ==="

echo "Wiping /memory/..."
"$MODAL" volume rm user-default-user /memory/ -r 2>/dev/null || true

echo "Wiping /session-storage/..."
"$MODAL" volume rm user-default-user /session-storage/ -r 2>/dev/null || true

echo "Wiping /session-transcripts/..."
"$MODAL" volume rm user-default-user /session-transcripts/ -r 2>/dev/null || true

echo "Restoring default prompts..."
"$SCRIPT_DIR/reset_prompts.sh"

echo "=== Factory reset complete ==="

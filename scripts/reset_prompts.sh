#!/usr/bin/env bash
# Reset prompt files on Modal volume to local defaults.
#
# Usage:
#   ./scripts/reset_prompts.sh                          # all prompts
#   ./scripts/reset_prompts.sh BOOTSTRAP.md             # single file
#   ./scripts/reset_prompts.sh BOOTSTRAP.md IDENTITY.md # multiple files

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PROMPTS_DIR="$PROJECT_DIR/prompts"
MODAL="$PROJECT_DIR/.venv/bin/modal"
VOLUME_PATH="/default-user/prompts"

if [ $# -eq 0 ]; then
  files=("$PROMPTS_DIR"/*.md)
else
  files=()
  for name in "$@"; do
    path="$PROMPTS_DIR/$name"
    if [ ! -f "$path" ]; then
      echo "Error: $name not found in prompts/" >&2
      exit 1
    fi
    files+=("$path")
  done
fi

for f in "${files[@]}"; do
  name="$(basename "$f")"
  echo "Uploading $name..."
  "$MODAL" volume put user-default-user "$f" "$VOLUME_PATH/$name" --force
done

echo "Done."

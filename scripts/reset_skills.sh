#!/usr/bin/env bash
# Reset skill files on Modal volume to local defaults.
#
# Usage:
#   ./scripts/reset_skills.sh              # all skills
#   ./scripts/reset_skills.sh pdf          # single skill
#   ./scripts/reset_skills.sh pdf pptx     # multiple skills

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SKILLS_DIR="$PROJECT_DIR/skills"
MODAL="$PROJECT_DIR/.venv/bin/modal"
VOLUME_PATH="/skills"

if [ $# -eq 0 ]; then
  # Wipe all skills when uploading all
  echo "Wiping existing skills..."
  "$MODAL" volume rm user-dev "$VOLUME_PATH" -r 2>/dev/null || true
  # Upload all skill directories
  dirs=()
  for d in "$SKILLS_DIR"/*/; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    [ "$name" = "__pycache__" ] && continue
    dirs+=("$d")
  done
else
  # Only wipe specified skills
  for name in "$@"; do
    echo "Wiping $name..."
    "$MODAL" volume rm user-dev "$VOLUME_PATH/$name" -r 2>/dev/null || true
  done
  dirs=()
  for name in "$@"; do
    path="$SKILLS_DIR/$name"
    if [ ! -d "$path" ]; then
      echo "Error: $name not found in skills/" >&2
      exit 1
    fi
    dirs+=("$path")
  done
fi

for d in "${dirs[@]}"; do
  name="$(basename "$d")"
  echo "Uploading $name/..."
  "$MODAL" volume put user-dev "$d" "$VOLUME_PATH/$name" --force
done

echo "Done."

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

# Generate manifest.json from all skills (uses Python for proper JSON escaping)
echo "Generating manifest.json..."
MANIFEST_TMP="$(mktemp)"
python3 -c "
import json, re, pathlib, sys

skills_dir = pathlib.Path(sys.argv[1])
manifest = []
for skill_dir in sorted(skills_dir.iterdir()):
    if not skill_dir.is_dir() or skill_dir.name == '__pycache__':
        continue
    skill_file = skill_dir / 'SKILL.md'
    if not skill_file.exists():
        continue
    # Parse YAML frontmatter
    text = skill_file.read_text()
    fm = re.match(r'^---\s*\n(.*?)\n---', text, re.DOTALL)
    if not fm:
        continue
    meta = {}
    for line in fm.group(1).splitlines():
        m = re.match(r'^(\w+):\s*(.+)$', line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
    if 'name' in meta and 'description' in meta:
        manifest.append({'name': meta['name'], 'description': meta['description']})

json.dump(manifest, open(sys.argv[2], 'w'))
print(f'  {len(manifest)} skills')
" "$SKILLS_DIR" "$MANIFEST_TMP"
# Upload into _manifest/ directory so it's picked up by folder count check
MANIFEST_DIR="$(mktemp -d)"
mkdir -p "$MANIFEST_DIR/_manifest"
mv "$MANIFEST_TMP" "$MANIFEST_DIR/_manifest/manifest.json"
"$MODAL" volume rm user-dev "$VOLUME_PATH/_manifest" -r 2>/dev/null || true
"$MODAL" volume put user-dev "$MANIFEST_DIR/_manifest" "$VOLUME_PATH/_manifest" --force
rm -rf "$MANIFEST_DIR"

echo "Done."

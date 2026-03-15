#!/usr/bin/env bash
# Factory reset: wipe LangGraph threads and Modal volume, restore expected structure.
# Disconnects all Composio accounts, deletes all triggers, cleans vault secrets.
#
# Usage:
#   ./scripts/factory_reset.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
MODAL="$PROJECT_DIR/venv/bin/modal"
PYTHON="$PROJECT_DIR/venv/bin/python"

KEEPFILE=$(mktemp)
touch "$KEEPFILE"
trap "rm -f $KEEPFILE" EXIT

echo "=== Factory Reset ==="

# Step 0: Truncate Supabase memory index (pgvector)
echo "Truncating memory_chunks in Supabase..."
"$PYTHON" -c "
import os
from dotenv import load_dotenv
load_dotenv(os.path.join('$PROJECT_DIR', '.env'))
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])
sb.table('memory_chunks').delete().neq('id', 0).execute()
print('  memory_chunks truncated')
" 2>&1 || echo "  Warning: failed to truncate memory_chunks (table may not exist yet)"

# Step 1: Disconnect all Composio accounts and delete all triggers
echo "Disconnecting all Composio accounts and triggers..."
"$PYTHON" -c "
import os, sys
from dotenv import load_dotenv
load_dotenv(os.path.join('$PROJECT_DIR', '.env'))
sys.path.insert(0, os.path.join('$PROJECT_DIR', 'src'))
from agent.auth import disconnect_all_services, teardown_all_triggers

results = teardown_all_triggers()
for r in results:
    print(f'  trigger: {r}')

results = disconnect_all_services()
for r in results:
    print(f'  service: {r}')
" 2>&1 || echo "  Warning: failed to disconnect Composio accounts"

# Step 2: Clean vault secrets (Slack bot, channels, etc.)
echo "Cleaning vault secrets..."
"$PYTHON" -c "
import os
from dotenv import load_dotenv
load_dotenv(os.path.join('$PROJECT_DIR', '.env'))
from supabase import create_client
sb = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])
for name in ['slack_bot_token', 'slack_bot_user_id', 'slack_signing_secret', 'slack_bot_owner_id', 'inbound_channels']:
    try:
        sb.rpc('delete_vault_secret', {'p_name': name}).execute()
        print(f'  deleted {name}')
    except:
        pass
" 2>&1 || echo "  Warning: failed to clean vault secrets"

# Step 3: Wipe LangGraph threads (local dev storage)
echo "Wiping LangGraph threads..."
rm -rf "$PROJECT_DIR/.langgraph_api"

# Step 4: Wipe everything on the volume
echo "Wiping entire volume..."
for item in $("$MODAL" volume ls user-dev / 2>/dev/null); do
  if [ "$item" = "." ] || [ "$item" = ".." ]; then continue; fi
  "$MODAL" volume rm user-dev "/$item" -r 2>/dev/null || \
  "$MODAL" volume rm user-dev "/$item" 2>/dev/null || true
done

# Step 5: Recreate empty directory structure
echo "Creating directory structure..."
for dir in memory session-storage session-transcripts .temp-uploads browser-profiles; do
  "$MODAL" volume put user-dev "$KEEPFILE" "/$dir/.keep" --force
done

# Step 6: Restore default config (user + heartbeat only)
echo "Restoring default config..."
"$MODAL" volume put user-dev "$PROJECT_DIR/config.default.json" /config.json --force

# Step 7: Restore default prompts
echo "Restoring default prompts..."
"$SCRIPT_DIR/reset_prompts.sh"

# Step 8: Upload scripts to volume
echo "Uploading scripts..."
"$MODAL" volume put user-dev "$SCRIPT_DIR/fetch_auth.py" /scripts/fetch_auth.py --force

# Step 9: Restore core skills only (weather, docx, pdf, pptx, xlsx)
echo "Restoring core skills..."
"$SCRIPT_DIR/reset_skills.sh" weather docx pdf pptx xlsx

# Step 10: Reset heartbeat cron
echo "Resetting heartbeat cron..."
"$PYTHON" "$SCRIPT_DIR/reset_heartbeat_cron.py"

echo "=== Factory reset complete ==="

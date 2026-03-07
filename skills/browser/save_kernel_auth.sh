#!/usr/bin/env bash
# save_kernel_auth.sh — Extract auth state from a Kernel headed browser session
# and save it to a local JSON file compatible with agent-browser's `state load`.
#
# Usage:
#   save_kernel_auth.sh <cdp_ws_url> <output_path>
#
# Example:
#   save_kernel_auth.sh "wss://browser-abc.kernel.sh/devtools/browser/xyz" /mnt/browser-profiles/gmail.json
#
# Strategy:
#   1. Primary: agent-browser --cdp <url> state save <path>
#   2. Fallback: Extract cookies/localStorage via CDP protocol directly using
#      Node.js (always available in sandbox), then write Playwright-compatible
#      storage state JSON that agent-browser can load.

set -euo pipefail

CDP_URL="${1:?Usage: save_kernel_auth.sh <cdp_ws_url> <output_path>}"
OUTPUT_PATH="${2:?Usage: save_kernel_auth.sh <cdp_ws_url> <output_path>}"

# Ensure output directory exists
mkdir -p "$(dirname "$OUTPUT_PATH")"

echo "[save_kernel_auth] Attempting primary method: agent-browser --cdp ..."

if agent-browser --cdp "$CDP_URL" state save "$OUTPUT_PATH" 2>/dev/null; then
  if [ -s "$OUTPUT_PATH" ]; then
    echo "[save_kernel_auth] OK — saved via agent-browser --cdp"
    exit 0
  fi
fi

echo "[save_kernel_auth] Primary method failed, using CDP fallback ..."

# Fallback: connect to CDP WebSocket directly with Node.js and extract state.
# This produces a Playwright-compatible storage state JSON file.
node --input-type=module <<'NODESCRIPT'
import { createRequire } from 'module';
import { writeFileSync } from 'fs';

const cdpUrl = process.argv[1] || process.env.CDP_URL;
const outputPath = process.argv[2] || process.env.OUTPUT_PATH;

if (!cdpUrl || !outputPath) {
  console.error("Missing CDP_URL or OUTPUT_PATH");
  process.exit(1);
}

// Minimal CDP client using raw WebSocket
// We use the ws package if available, otherwise fall back to undici or native
let WebSocket;
try {
  WebSocket = (await import('ws')).default;
} catch {
  // Node 22+ has built-in WebSocket
  WebSocket = globalThis.WebSocket;
}

if (!WebSocket) {
  console.error("No WebSocket implementation available");
  process.exit(1);
}

const ws = new WebSocket(cdpUrl);
let msgId = 1;
const pending = new Map();

ws.on('message', (data) => {
  const msg = JSON.parse(data.toString());
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
  }
});

function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = msgId++;
    const msg = { id, method, params };
    if (sessionId) msg.sessionId = sessionId;
    pending.set(id, resolve);
    ws.send(JSON.stringify(msg));
    setTimeout(() => { pending.delete(id); reject(new Error(`Timeout: ${method}`)); }, 15000);
  });
}

await new Promise((resolve) => ws.on('open', resolve));

// Get all browser targets to find pages
const { result: { targetInfos } } = await send('Target.getTargets');
const pages = targetInfos.filter(t => t.type === 'page');

if (pages.length === 0) {
  console.error("No page targets found");
  ws.close();
  process.exit(1);
}

// Attach to the first page to get sessionId
const { result: { sessionId } } = await send('Target.attachToTarget', {
  targetId: pages[0].targetId,
  flatten: true,
});

// 1. Get all cookies
const { result: { cookies } } = await send('Network.getAllCookies', {}, sessionId);

// Convert CDP cookies to Playwright format
const playwrightCookies = cookies.map(c => ({
  name: c.name,
  value: c.value,
  domain: c.domain,
  path: c.path,
  expires: c.expires || -1,
  httpOnly: c.httpOnly || false,
  secure: c.secure || false,
  sameSite: (c.sameSite || 'None'),
}));

// 2. Get localStorage from all frames
const origins = [];
try {
  // Get current page URL for origin
  const { result } = await send('Runtime.evaluate', {
    expression: `JSON.stringify({
      origin: window.location.origin,
      items: Object.keys(localStorage).map(k => ({name: k, value: localStorage.getItem(k)}))
    })`,
    returnByValue: false,
  }, sessionId);

  if (result?.result?.value) {
    const parsed = JSON.parse(result.result.value);
    if (parsed.items.length > 0) {
      origins.push(parsed);
    }
  }
} catch (e) {
  // localStorage extraction is best-effort
  console.error("[save_kernel_auth] localStorage extraction failed (non-fatal):", e.message);
}

// Build Playwright-compatible storage state
const storageState = {
  cookies: playwrightCookies,
  origins: origins.map(o => ({
    origin: o.origin,
    localStorage: o.items,
  })),
};

writeFileSync(outputPath, JSON.stringify(storageState, null, 2));
console.log(`[save_kernel_auth] OK — saved ${playwrightCookies.length} cookies via CDP fallback`);

ws.close();
NODESCRIPT

# Check output was written
if [ -s "$OUTPUT_PATH" ]; then
  echo "[save_kernel_auth] Auth state saved to $OUTPUT_PATH"
  exit 0
else
  echo "[save_kernel_auth] ERROR — failed to save auth state" >&2
  exit 1
fi

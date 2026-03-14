#!/usr/bin/env node
// Browserbase browser manager for context-based cookie persistence.
// Usage:
//   node browserbase_browser.js create-context <name>
//   node browserbase_browser.js create-session <context-id> [--persist]
//   node browserbase_browser.js live-view <session-id>
//   node browserbase_browser.js close-session <session-id>
//
// All sessions use a residential proxy by default.
// Requires BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID env vars.
// Outputs JSON to stdout. Errors go to stderr.

const API = "https://api.browserbase.com/v1";
const API_KEY = process.env.BROWSERBASE_API_KEY;
const PROJECT_ID = process.env.BROWSERBASE_PROJECT_ID;

function headers() {
  return { "X-BB-API-Key": API_KEY, "Content-Type": "application/json" };
}

async function createContext(name) {
  const res = await fetch(`${API}/contexts`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ projectId: PROJECT_ID }),
  });
  if (!res.ok) throw new Error(`Create context failed: ${res.status} ${await res.text()}`);
  const data = await res.json();
  console.log(JSON.stringify({ context_id: data.id, name }));
}

async function createSession(contextId, persist) {
  const body = {
    projectId: PROJECT_ID,
    proxies: true,
    browserSettings: {},
  };
  if (contextId) {
    body.browserSettings.context = { id: contextId, persist: !!persist };
  }
  const res = await fetch(`${API}/sessions`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Create session failed: ${res.status} ${await res.text()}`);
  const data = await res.json();
  console.log(
    JSON.stringify({
      session_id: data.id,
      connect_url: data.connectUrl,
    })
  );
}

async function liveView(sessionId) {
  const res = await fetch(`${API}/sessions/${sessionId}/debug`, {
    headers: headers(),
  });
  if (!res.ok) throw new Error(`Live view failed: ${res.status} ${await res.text()}`);
  const data = await res.json();
  console.log(
    JSON.stringify({
      live_view_url: data.debuggerFullscreenUrl,
      pages: data.pages,
    })
  );
}

async function closeSession(sessionId) {
  const res = await fetch(`${API}/sessions/${sessionId}`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ status: "REQUEST_RELEASE", projectId: PROJECT_ID }),
  });
  if (!res.ok) throw new Error(`Close session failed: ${res.status} ${await res.text()}`);
  console.log(JSON.stringify({ closed: sessionId }));
}

async function main() {
  if (!API_KEY) { console.error("BROWSERBASE_API_KEY not set"); process.exit(1); }
  if (!PROJECT_ID) { console.error("BROWSERBASE_PROJECT_ID not set"); process.exit(1); }

  const [, , command, ...args] = process.argv;

  switch (command) {
    case "create-context": {
      const name = args[0] || "default";
      await createContext(name);
      break;
    }
    case "create-session": {
      const contextId = args[0];
      const persist = args.includes("--persist");
      await createSession(contextId, persist);
      break;
    }
    case "live-view": {
      const sessionId = args[0];
      if (!sessionId) { console.error("Usage: live-view <session-id>"); process.exit(1); }
      await liveView(sessionId);
      break;
    }
    case "close-session": {
      const sessionId = args[0];
      if (!sessionId) { console.error("Usage: close-session <session-id>"); process.exit(1); }
      await closeSession(sessionId);
      break;
    }
    default:
      console.error("Commands: create-context, create-session, live-view, close-session");
      process.exit(1);
  }
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});

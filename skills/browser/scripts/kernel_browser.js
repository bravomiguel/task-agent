#!/usr/bin/env node
// Kernel browser manager using @onkernel/sdk for native profile persistence.
// Usage:
//   node kernel_browser.js create <profile-name> [--stealth] [--headless] [--save-changes] [--no-proxy]
//   node kernel_browser.js delete <session-id>
//
// All sessions use a US residential proxy and 30s idle timeout by default.
// Use --no-proxy to disable the proxy.
//
// Outputs JSON to stdout. Errors go to stderr.

const Kernel = require("@onkernel/sdk").default;
const { ConflictError } = require("@onkernel/sdk");

const kernel = new Kernel();

async function ensureProfile(name) {
  try {
    return await kernel.profiles.create({ name });
  } catch (err) {
    if (err instanceof ConflictError) {
      // Profile already exists — fetch it by name
      return await kernel.profiles.retrieve(name);
    }
    throw err;
  }
}

async function ensureProxy() {
  try {
    return await kernel.proxies.create({
      type: "residential",
      name: "US",
      config: { country: "US" },
    });
  } catch (err) {
    if (err instanceof ConflictError) {
      // Proxy already exists — list and find it
      const proxies = await kernel.proxies.list();
      const existing = proxies.data
        ? proxies.data.find((p) => p.name === "US")
        : Array.isArray(proxies)
          ? proxies.find((p) => p.name === "US")
          : null;
      if (existing) return existing;
    }
    throw err;
  }
}

async function createBrowser(profileName, opts) {
  const profile = await ensureProfile(profileName);
  const params = {
    profile: {
      name: profileName,
      save_changes: opts.saveChanges ?? false,
    },
    timeout_seconds: 60,
  };
  if (opts.stealth) params.stealth = true;
  if (opts.headless) params.headless = true;

  if (!opts.noProxy) {
    const proxy = await ensureProxy();
    params.proxy_id = proxy.id;
  }

  const browser = await kernel.browsers.create(params);
  console.log(
    JSON.stringify({
      session_id: browser.session_id,
      cdp_ws_url: browser.cdp_ws_url,
      browser_live_view_url: browser.browser_live_view_url,
      profile_name: profileName,
    })
  );
}

async function deleteBrowser(sessionId) {
  await kernel.browsers.deleteByID(sessionId);
  console.log(JSON.stringify({ deleted: sessionId }));
}

async function main() {
  const [, , command, ...args] = process.argv;

  switch (command) {
    case "create": {
      const profileName = args[0];
      if (!profileName) {
        console.error(
          "Usage: create <profile-name> [--stealth] [--headless] [--save-changes] [--no-proxy]"
        );
        process.exit(1);
      }
      const flags = new Set(args.slice(1));
      await createBrowser(profileName, {
        stealth: flags.has("--stealth"),
        headless: flags.has("--headless"),
        saveChanges: flags.has("--save-changes"),
        noProxy: flags.has("--no-proxy"),
      });
      break;
    }
    case "delete": {
      const sessionId = args[0];
      if (!sessionId) {
        console.error("Usage: delete <session-id>");
        process.exit(1);
      }
      await deleteBrowser(sessionId);
      break;
    }
    default:
      console.error("Commands: create, delete");
      process.exit(1);
  }
}

main().catch((err) => {
  console.error(err.message || err);
  process.exit(1);
});

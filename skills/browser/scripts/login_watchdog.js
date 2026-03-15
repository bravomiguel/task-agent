#!/usr/bin/env node
// Login watchdog — monitors a Kernel headed session for inactivity and kills it if abandoned.
// Runs in the background. Takes screenshots every 10 seconds via agent-browser.
// If 6 consecutive "stale" verdicts (1 minute), kills the Kernel session.
//
// Uses GPT-4o-mini vision with structured output, image content blocks,
// and cumulative context (previous messages auto-cached by OpenAI API).
//
// Usage: node login_watchdog.js <session-id> &
//
// Requires OPENAI_API_KEY env var.

const { execSync } = require("child_process");
const { readFileSync, unlinkSync } = require("fs");

const SESSION_ID = process.argv[2];
if (!SESSION_ID) {
  console.error("Usage: login_watchdog.js <session-id>");
  process.exit(1);
}

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
if (!OPENAI_API_KEY) {
  console.error("[watchdog] OPENAI_API_KEY not set, exiting");
  process.exit(1);
}

const POLL_INTERVAL_MS = 10_000;
const MAX_STALE_COUNT = 6; // 6 × 10s = 1 minute
const SCREENSHOT_PATH = "/tmp/watchdog_screenshot.png";

const SYSTEM_PROMPT = {
  role: "system",
  content:
    "You are monitoring a login page via screenshots taken 10 seconds apart. " +
    "Determine if the page shows meaningful change since the previous screenshot " +
    "(text typed in fields, page navigated, 2FA prompt, CAPTCHA interaction, new content visible, etc). " +
    "Ignore minor differences like cursor blinks, animations, or loading spinners.",
};

const RESPONSE_FORMAT = {
  type: "json_schema",
  json_schema: {
    name: "login_status",
    strict: true,
    schema: {
      type: "object",
      properties: {
        status: {
          type: "string",
          enum: ["active", "stale"],
          description:
            "active = meaningful change detected since last screenshot, stale = no meaningful change",
        },
      },
      required: ["status"],
      additionalProperties: false,
    },
  },
};

// Cumulative conversation — grows each poll, previous messages auto-cached by API
const messages = [SYSTEM_PROMPT];
let staleCount = 0;
let pollCount = 0;

function log(msg) {
  console.error(`[watchdog] ${msg}`);
}

function takeScreenshot() {
  try {
    execSync(`agent-browser screenshot ${SCREENSHOT_PATH}`, {
      timeout: 15_000,
      stdio: ["pipe", "pipe", "pipe"],
    });
    const buffer = readFileSync(SCREENSHOT_PATH);
    unlinkSync(SCREENSHOT_PATH);
    return buffer.toString("base64");
  } catch {
    return null;
  }
}

function imageMessage(base64) {
  return {
    role: "user",
    content: [
      {
        type: "image_url",
        image_url: {
          url: `data:image/png;base64,${base64}`,
          detail: "low",
        },
      },
    ],
  };
}

async function checkStatus(base64) {
  messages.push(imageMessage(base64));

  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: "gpt-4o-mini",
      max_tokens: 20,
      messages,
      response_format: RESPONSE_FORMAT,
    }),
  });

  if (!res.ok) {
    log(`poll ${pollCount}: API error (${res.status}), assuming active`);
    staleCount = 0;
    return;
  }

  const data = await res.json();
  const content = data.choices?.[0]?.message?.content || "{}";

  // Add assistant reply to conversation for context continuity
  messages.push({ role: "assistant", content });

  try {
    const parsed = JSON.parse(content);
    if (parsed.status === "stale") {
      staleCount++;
      log(`poll ${pollCount}: stale (${staleCount}/${MAX_STALE_COUNT})`);
    } else {
      staleCount = 0;
      log(`poll ${pollCount}: active`);
    }
  } catch {
    staleCount = 0;
  }
}

function killSession() {
  try {
    execSync("agent-browser close", { timeout: 10_000, stdio: "pipe" });
  } catch {}
  try {
    execSync(
      `node /mnt/skills/browser/scripts/kernel_browser.js delete "${SESSION_ID}"`,
      { timeout: 15_000, stdio: "pipe" }
    );
  } catch {}
}

async function main() {
  log(`started for session ${SESSION_ID}`);

  // Initial screenshot (baseline — always "active")
  const initial = takeScreenshot();
  if (!initial) {
    log("no initial screenshot, exiting");
    process.exit(0);
  }
  messages.push(imageMessage(initial));
  messages.push({ role: "assistant", content: '{"status":"active"}' });
  log("baseline screenshot captured");

  // Poll loop
  while (true) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));

    pollCount++;
    const base64 = takeScreenshot();
    if (!base64) {
      log("browser closed externally, exiting");
      process.exit(0);
    }

    await checkStatus(base64);

    if (staleCount >= MAX_STALE_COUNT) {
      log(`killed session ${SESSION_ID} (inactive 60s)`);
      killSession();
      process.exit(0);
    }
  }
}

main().catch(() => process.exit(1));

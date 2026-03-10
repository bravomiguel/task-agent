/**
 * channel-webhook — Supabase Edge Function
 *
 * Receives inbound Slack messages via two routes:
 *   POST /channel-webhook/slack      — Composio SLACK_NEW_MESSAGE trigger (user OAuth)
 *   POST /channel-webhook/slack-bot  — Slack Events API (bot events)
 *
 * Both routes buffer messages in `inbound_buffer` with debounce, then flush
 * to `inbound_queue` for dispatch. Same channel buffer key for cross-source dedup.
 *
 * Bot DMs use 300ms debounce for fast response. Everything else uses 5s.
 *
 * Required env vars:
 *   COMPOSIO_WEBHOOK_SECRET — HMAC secret for Composio signature verification
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 *
 * Slack signing secret is stored per-user in Supabase vault (not as env var)
 * and looked up at request time.
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const COMPOSIO_WEBHOOK_SECRET = Deno.env.get("COMPOSIO_WEBHOOK_SECRET") ?? "";
const COMPOSIO_API_KEY = Deno.env.get("COMPOSIO_API_KEY") ?? "";
const COMPOSIO_ENTITY_ID = Deno.env.get("COMPOSIO_ENTITY_ID") ?? "default";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const DEBOUNCE_MS = 5_000;
const BOT_DM_DEBOUNCE_MS = 300;

// ---------------------------------------------------------------------------
// Composio HMAC verification
// ---------------------------------------------------------------------------

async function verifyComposioSignature(
  rawBody: string,
  webhookId: string,
  webhookTimestamp: string,
  signatureHeader: string,
): Promise<boolean> {
  if (!COMPOSIO_WEBHOOK_SECRET || !signatureHeader) return false;

  // Signing string: "{webhook_id}.{webhook_timestamp}.{raw_body}"
  const signingString = `${webhookId}.${webhookTimestamp}.${rawBody}`;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(COMPOSIO_WEBHOOK_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(signingString),
  );
  const computedSig = btoa(String.fromCharCode(...new Uint8Array(sig)));

  // Signature header format: "v1,{base64}" — extract the base64 part
  const received = signatureHeader.includes(",")
    ? signatureHeader.split(",")[1]
    : signatureHeader;

  return computedSig === received;
}

// ---------------------------------------------------------------------------
// Slack signing secret verification
// ---------------------------------------------------------------------------

async function verifySlackSignature(
  rawBody: string,
  timestamp: string,
  signature: string,
  signingSecret: string,
): Promise<boolean> {
  if (!signingSecret || !timestamp || !signature) return false;

  // Reject requests older than 5 minutes
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - Number(timestamp)) > 300) return false;

  const baseString = `v0:${timestamp}:${rawBody}`;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(signingSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(baseString),
  );
  const computed = "v0=" + Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  return computed === signature;
}

// ---------------------------------------------------------------------------
// Slack user name resolution
// ---------------------------------------------------------------------------

let _slackTokenCache: { token: string; expiresAt: number } | null = null;

async function getSlackTokenFromComposio(): Promise<string | null> {
  // Return cached token if still valid (cache for 5 minutes)
  if (_slackTokenCache && Date.now() < _slackTokenCache.expiresAt) {
    return _slackTokenCache.token;
  }

  if (!COMPOSIO_API_KEY) return null;

  try {
    const params = new URLSearchParams({
      statuses: "ACTIVE",
      user_ids: COMPOSIO_ENTITY_ID,
    });
    const resp = await fetch(
      `https://backend.composio.dev/api/v3/connected_accounts?${params}`,
      { headers: { "x-api-key": COMPOSIO_API_KEY } },
    );
    if (!resp.ok) return null;

    const data = await resp.json();
    const items = data.items ?? data;
    if (!Array.isArray(items)) return null;

    for (const item of items) {
      const slug = typeof item.toolkit === "object" ? item.toolkit?.slug : item.toolkit;
      if (slug === "slack" && item.status === "ACTIVE") {
        const token = item.state?.val?.access_token;
        if (token) {
          _slackTokenCache = { token, expiresAt: Date.now() + 5 * 60 * 1000 };
          return token;
        }
      }
    }
  } catch {
    // Fall through
  }
  return null;
}

async function resolveSlackUserName(userId: string): Promise<string> {
  // Try bot token from vault first, then Composio user token
  let token = await getVaultSecret("slack_bot_token");
  if (!token) {
    token = await getSlackTokenFromComposio();
  }
  if (!token) return userId;

  try {
    const resp = await fetch(`https://slack.com/api/users.info?user=${userId}`, {
      headers: { "Authorization": `Bearer ${token}` },
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data.ok) {
        return data.user?.real_name ?? data.user?.name ?? userId;
      }
    }
  } catch {
    // Fall through
  }
  return userId;
}

// ---------------------------------------------------------------------------
// Supabase REST helpers
// ---------------------------------------------------------------------------

function supabaseHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
  };
}

async function insertBuffer(row: Record<string, unknown>): Promise<Record<string, unknown> | null> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_buffer`, {
    method: "POST",
    headers: {
      ...supabaseHeaders(),
      "Prefer": "return=representation,resolution=ignore-duplicates",
    },
    body: JSON.stringify(row),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[channel-webhook] buffer insert failed ${resp.status}: ${text}`);
    return null;
  }
  const rows = await resp.json();
  return rows.length > 0 ? rows[0] : null;
}

async function checkNewerMessages(bufferKey: string, afterTs: string): Promise<boolean> {
  const params = new URLSearchParams({
    buffer_key: `eq.${bufferKey}`,
    created_at: `gt.${afterTs}`,
    order: "created_at.desc",
    limit: "1",
  });
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_buffer?${params}`, {
    headers: supabaseHeaders(),
  });
  if (!resp.ok) return false;
  const rows = await resp.json();
  return rows.length > 0;
}

async function flushBuffer(bufferKey: string): Promise<Record<string, unknown>[]> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/rpc/flush_inbound_buffer`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify({ p_buffer_key: bufferKey }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[channel-webhook] flush failed ${resp.status}: ${text}`);
    return [];
  }
  return await resp.json();
}

async function insertQueue(row: Record<string, unknown>): Promise<void> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_queue`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify(row),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[channel-webhook] queue insert failed ${resp.status}: ${text}`);
  }
}

async function getVaultSecret(name: string): Promise<string | null> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/rpc/get_vault_secret`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify({ p_name: name }),
  });
  if (!resp.ok) return null;
  const data = await resp.json();
  return data ?? null;
}

// ---------------------------------------------------------------------------
// Shared debounce + flush + queue logic
// ---------------------------------------------------------------------------

async function bufferAndFlush(
  senderId: string,
  senderName: string,
  messageText: string,
  channelId: string,
  channelType: string,
  messageTs: string,
  threadTs: string,
  debounceMs: number,
): Promise<Response> {
  const bufferKey = `slack:${channelId}`;
  const priority = channelType === "im" ? 1 : 2;

  // Step 1: Insert into buffer (dedup via unique index on buffer_key + message_ts)
  const inserted = await insertBuffer({
    source: "slack",
    buffer_key: bufferKey,
    sender: senderId,
    sender_name: senderName,
    message_text: messageText,
    metadata: {
      message_ts: messageTs,
      thread_ts: threadTs,
      channel_id: channelId,
      channel_type: channelType,
      priority,
    },
  });

  if (!inserted) {
    return jsonResponse({ ok: true, skipped: "duplicate_or_error" });
  }

  const insertedAt = inserted.created_at as string;

  // Step 2: Debounce — wait, then check if newer messages arrived
  await new Promise((r) => setTimeout(r, debounceMs));

  const hasNewer = await checkNewerMessages(bufferKey, insertedAt);
  if (hasNewer) {
    console.log(`[channel-webhook] ${bufferKey} debounce: newer messages exist, skipping flush`);
    return jsonResponse({ ok: true, debounced: true });
  }

  // Step 3: Flush — atomically grab all buffered messages for this key
  const flushedRows = await flushBuffer(bufferKey);
  if (flushedRows.length === 0) {
    return jsonResponse({ ok: true, skipped: "already_flushed" });
  }

  // Step 4: Combine into a single batch and insert into dispatch queue
  const senders = new Set<string>();
  const senderIds = new Set<string>();
  const lines: string[] = [];
  let lastThreadTs = "";

  for (const row of flushedRows) {
    const name = (row.sender_name as string) || (row.sender as string);
    senders.add(name);
    if (row.sender) senderIds.add(row.sender as string);
    const id = (row.sender as string) || "";
    lines.push(`[${name} (${id})] ${row.message_text}`);
    const meta = row.metadata as Record<string, unknown> | undefined;
    if (meta?.thread_ts) lastThreadTs = meta.thread_ts as string;
  }

  const combinedText = lines.join("\n");

  await insertQueue({
    source: "slack",
    priority,
    buffer_key: bufferKey,
    combined_text: combinedText,
    metadata: {
      channel_id: channelId,
      channel_type: channelType,
      thread_ts: lastThreadTs,
      senders: [...senders],
      sender_ids: [...senderIds],
      message_count: flushedRows.length,
    },
  });

  console.log(`[channel-webhook] ${bufferKey} flushed ${flushedRows.length} messages to queue`);
  return jsonResponse({ ok: true, flushed: true, count: flushedRows.length });
}

// ---------------------------------------------------------------------------
// Slack handler — Composio webhook (user OAuth)
// ---------------------------------------------------------------------------

async function handleSlack(req: Request): Promise<Response> {
  const rawBody = await req.text();

  // Verify Composio HMAC-SHA256 signature
  if (COMPOSIO_WEBHOOK_SECRET) {
    const webhookId = req.headers.get("webhook-id") ?? "";
    const webhookTimestamp = req.headers.get("webhook-timestamp") ?? "";
    const sigHeader = req.headers.get("webhook-signature") ?? "";
    if (sigHeader) {
      const valid = await verifyComposioSignature(rawBody, webhookId, webhookTimestamp, sigHeader);
      if (!valid) {
        return new Response("Invalid signature", { status: 401 });
      }
    }
  }

  let body: Record<string, unknown>;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  console.log("[channel-webhook/slack] Payload keys:", Object.keys(body));

  // Extract from Composio trigger payload
  const data = body.data as Record<string, unknown> | undefined;
  if (!data) {
    console.log("[channel-webhook/slack] No 'data' key in payload, keys:", Object.keys(body));
    return jsonResponse({ ok: true, skipped: "no_data" });
  }

  // V3 payload uses "text", V2 used "message"
  const messageText = (data.text as string) ?? (data.message as string) ?? "";
  if (!messageText) {
    console.log("[channel-webhook/slack] Empty message, data keys:", Object.keys(data));
    return jsonResponse({ ok: true, skipped: "empty_message" });
  }

  // V3 payload uses "user" (user ID string), V2 used "sender" object
  const senderObj = data.sender as Record<string, unknown> | undefined;
  const senderId = (data.user as string) ?? (senderObj?.id as string) ?? "unknown";

  // Filter out bot's own messages (Composio triggers on all channel messages)
  if (data.bot_id || data.bot_profile) {
    return jsonResponse({ ok: true, skipped: "bot_message" });
  }
  const botUserId = await getVaultSecret("slack_bot_user_id");
  if (botUserId && senderId === botUserId) {
    return jsonResponse({ ok: true, skipped: "self_message" });
  }

  // Resolve display name via Slack API (bot token or Composio user token)
  const senderName = (senderObj?.name as string) ?? await resolveSlackUserName(senderId);
  const channelId = (data.channel as string) ?? (data.channel_id as string) ?? "unknown";
  const channelType = (data.channel_type as string) ?? "";
  const messageTs = String(data.ts ?? data.timestamp ?? "");
  const threadTs = (data.thread_ts as string) ?? "";

  return bufferAndFlush(
    senderId, senderName, messageText,
    channelId, channelType, messageTs, threadTs,
    DEBOUNCE_MS,
  );
}

// ---------------------------------------------------------------------------
// Slack Bot handler — Slack Events API (direct from Slack)
// ---------------------------------------------------------------------------

async function handleSlackBot(req: Request): Promise<Response> {
  const rawBody = await req.text();

  // Parse body first — needed to handle url_verification before secret is stored,
  // and to identify team for future multi-tenant secret lookup.
  let body: Record<string, unknown>;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  // Handle URL verification challenge (required during app setup, before
  // signing secret is stored in vault — Slack sends this immediately on save)
  if (body.type === "url_verification") {
    return jsonResponse({ challenge: body.challenge as string });
  }

  // Look up signing secret from vault and verify request
  const signingSecret = await getVaultSecret("slack_signing_secret");
  if (signingSecret) {
    const timestamp = req.headers.get("x-slack-request-timestamp") ?? "";
    const signature = req.headers.get("x-slack-signature") ?? "";
    const valid = await verifySlackSignature(rawBody, timestamp, signature, signingSecret);
    if (!valid) {
      return new Response("Invalid signature", { status: 401 });
    }
  }

  // Short-circuit Slack retries (we handle dedup via buffer)
  const retryNum = req.headers.get("x-slack-retry-num");
  if (retryNum) {
    return jsonResponse({ ok: true, skipped: "retry" });
  }

  if (body.type !== "event_callback") {
    return jsonResponse({ ok: true, skipped: "not_event_callback" });
  }

  const event = body.event as Record<string, unknown> | undefined;
  if (!event) {
    return jsonResponse({ ok: true, skipped: "no_event" });
  }

  const eventType = event.type as string;
  if (!["message", "app_mention"].includes(eventType)) {
    return jsonResponse({ ok: true, skipped: "unsupported_event_type" });
  }

  // Skip bot's own messages and message_changed/deleted subtypes
  const subtype = event.subtype as string | undefined;
  if (subtype) {
    return jsonResponse({ ok: true, skipped: "subtype" });
  }

  // Filter out bot messages (bot_id present means it's from a bot)
  if (event.bot_id) {
    return jsonResponse({ ok: true, skipped: "bot_message" });
  }

  // Also check against our own bot_user_id from vault
  const botUserId = await getVaultSecret("slack_bot_user_id");
  if (botUserId && event.user === botUserId) {
    return jsonResponse({ ok: true, skipped: "self_message" });
  }

  const messageText = (event.text as string) ?? "";
  if (!messageText) {
    return jsonResponse({ ok: true, skipped: "empty_message" });
  }

  const senderId = (event.user as string) ?? "unknown";
  const channelId = (event.channel as string) ?? "unknown";
  const channelType = (event.channel_type as string) ?? "";
  const messageTs = (event.ts as string) ?? "";
  const threadTs = (event.thread_ts as string) ?? "";

  // Block non-owner DMs (personal workspace use case)
  if (channelType === "im") {
    const ownerId = await getVaultSecret("slack_bot_owner_id");
    if (ownerId && senderId !== ownerId) {
      // Reply with a rejection message, then skip processing
      const rejectToken = await getVaultSecret("slack_bot_token");
      if (rejectToken) {
        await fetch("https://slack.com/api/chat.postMessage", {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${rejectToken}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            channel: channelId,
            text: "Sorry, this is a private assistant. Only the owner can message me directly.",
          }),
        });
      }
      return jsonResponse({ ok: true, skipped: "non_owner_dm" });
    }
  }

  // Resolve sender display name via Slack API
  let senderName = senderId;
  const botToken = await getVaultSecret("slack_bot_token");
  if (botToken) {
    try {
      const userResp = await fetch(`https://slack.com/api/users.info?user=${senderId}`, {
        headers: { "Authorization": `Bearer ${botToken}` },
      });
      if (userResp.ok) {
        const userData = await userResp.json();
        if (userData.ok) {
          senderName = userData.user?.real_name ?? userData.user?.name ?? senderId;
        }
      }
    } catch {
      // Fall through with senderId as name
    }
  }

  // Bot DMs get fast debounce, channels get standard
  const debounceMs = channelType === "im" ? BOT_DM_DEBOUNCE_MS : DEBOUNCE_MS;

  return bufferAndFlush(
    senderId, senderName, messageText,
    channelId, channelType, messageTs, threadTs,
    debounceMs,
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function jsonResponse(data: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const url = new URL(req.url);
  const path = url.pathname;

  if (path.endsWith("/slack-bot")) {
    return handleSlackBot(req);
  }
  if (path.endsWith("/slack")) {
    return handleSlack(req);
  }

  return jsonResponse({ error: "Unknown platform. Use /slack or /slack-bot" }, 404);
});

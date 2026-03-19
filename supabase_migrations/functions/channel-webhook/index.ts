/**
 * channel-webhook — Supabase Edge Function
 *
 * Receives inbound messages from external platforms via routes:
 *   POST /channel-webhook/composio   — All Composio triggers (Slack, Gmail, Outlook)
 *   POST /channel-webhook/slack      — Alias for /composio (backwards compat)
 *   POST /channel-webhook/slack-bot  — Slack Events API (bot events, direct from Slack)
 *   GET  /channel-webhook/teams      — Microsoft Graph subscription validation
 *   POST /channel-webhook/teams      — Microsoft Graph change notifications (Teams messages)
 *   POST /channel-webhook/meetings   — Meeting transcripts from meeting-recorder app
 *
 * Composio sends all triggers to a single webhook URL. The handler inspects
 * payload.type to route: slack triggers → buffer+flush, email triggers → direct queue.
 *
 * Teams uses Microsoft Graph change notifications (subscriptions). The webhook
 * receives lightweight notifications, then fetches full message content via
 * Graph API using a Composio-managed Microsoft token.
 *
 * Required env vars:
 *   COMPOSIO_WEBHOOK_SECRET — HMAC secret for Composio signature verification
 *   COMPOSIO_API_KEY — for fetching Composio connected account tokens
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 *
 * Slack signing secret is stored per-user in Supabase vault (not as env var)
 * and looked up at request time.
 *
 * Teams webhook secret (clientState) is stored in vault as "teams_webhook_secret".
 * Teams Composio connection ID is stored in vault as "teams_composio_connection_id".
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const COMPOSIO_WEBHOOK_SECRET = Deno.env.get("COMPOSIO_WEBHOOK_SECRET") ?? "";
const COMPOSIO_API_KEY = Deno.env.get("COMPOSIO_API_KEY") ?? "";
const COMPOSIO_ENTITY_ID = Deno.env.get("COMPOSIO_ENTITY_ID") ?? "default";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const DEBOUNCE_MS = 5_000;
const BOT_DM_DEBOUNCE_MS = 300;

// Composio trigger type constants (lowercase for case-insensitive matching)
const SLACK_TRIGGERS = [
  "slack_receive_direct_message",
  "slack_receive_message",
  "slack_receive_thread_reply",
  "slack_receive_group_message",
  "slack_receive_mpim_message",
  "slack_new_message",
];

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

  // Signature header may contain multiple signatures: "v1,{base64} v1,{base64}"
  const signatures = signatureHeader.split(" ").map((s) =>
    s.includes(",") ? s.split(",")[1] : s
  );
  return signatures.some((s) => s === computedSig);
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
// Composio token helpers (for Outlook email fetching)
// ---------------------------------------------------------------------------

async function fetchComposioAccessToken(connectionNanoId: string): Promise<string | null> {
  if (!COMPOSIO_API_KEY) return null;
  try {
    const resp = await fetch(
      `https://backend.composio.dev/api/v3/connected_accounts/${connectionNanoId}`,
      { headers: { "x-api-key": COMPOSIO_API_KEY } },
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    return data?.data?.access_token ?? null;
  } catch {
    return null;
  }
}

async function fetchOutlookEmail(
  messageId: string,
  accessToken: string,
): Promise<{ subject: string; body: string; from: string; to: string; cc: string; bcc: string; attachments: Array<{ name: string; size: number }> } | null> {
  try {
    const resp = await fetch(
      `https://graph.microsoft.com/v1.0/me/messages/${encodeURIComponent(messageId)}?$select=subject,body,bodyPreview,from,toRecipients,ccRecipients,bccRecipients,hasAttachments&$expand=attachments($select=name,size)`,
      {
        headers: {
          Authorization: `Bearer ${accessToken}`,
          "Content-Type": "application/json",
          Prefer: 'outlook.body-content-type="text"',
        },
      },
    );
    if (!resp.ok) return null;
    const email = await resp.json();
    const extractAddresses = (arr: Array<{ emailAddress?: { address?: string } }> | undefined) =>
      (arr ?? []).map((r) => r.emailAddress?.address).filter(Boolean).join(", ");
    const attachments = (email.attachments ?? []).map((a: Record<string, unknown>) => ({
      name: (a.name as string) ?? "unnamed",
      size: (a.size as number) ?? 0,
    }));
    return {
      subject: email.subject ?? "",
      body: email.body?.content ?? email.bodyPreview ?? "",
      from: email.from?.emailAddress?.address ?? "",
      to: extractAddresses(email.toRecipients),
      cc: extractAddresses(email.ccRecipients),
      bcc: extractAddresses(email.bccRecipients),
      attachments,
    };
  } catch {
    return null;
  }
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
  via: "chat_surface" | "connection" = "connection",
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
      via,
      senders: [...senders],
      sender_ids: [...senderIds],
      message_count: flushedRows.length,
    },
  });

  console.log(`[channel-webhook] ${bufferKey} flushed ${flushedRows.length} messages to queue`);
  return jsonResponse({ ok: true, flushed: true, count: flushedRows.length });
}

// ---------------------------------------------------------------------------
// Inbound channel gate — check if platform is enabled via vault config
// ---------------------------------------------------------------------------

let _channelsCache: { config: Record<string, boolean>; expiresAt: number } | null = null;

async function isChannelEnabled(platform: string): Promise<boolean> {
  // Cache for 60 seconds to avoid hammering vault on every message
  if (_channelsCache && Date.now() < _channelsCache.expiresAt) {
    return _channelsCache.config[platform] !== false;
  }

  const raw = await getVaultSecret("inbound_channels");
  if (raw) {
    try {
      const config = JSON.parse(raw) as Record<string, boolean>;
      _channelsCache = { config, expiresAt: Date.now() + 60_000 };
      return config[platform] !== false;
    } catch {
      // Invalid JSON — fall through to default (enabled)
    }
  }
  // No config or parse error — all channels enabled by default
  return true;
}

// ---------------------------------------------------------------------------
// Composio handler — unified entry point for all Composio triggers
// ---------------------------------------------------------------------------

async function handleComposio(req: Request): Promise<Response> {
  const rawBody = await req.text();

  // Verify Composio HMAC-SHA256 signature
  if (COMPOSIO_WEBHOOK_SECRET) {
    const webhookId = req.headers.get("webhook-id") ?? "";
    const webhookTimestamp = req.headers.get("webhook-timestamp") ?? "";
    const sigHeader = req.headers.get("webhook-signature") ?? "";
    if (sigHeader) {
      const valid = await verifyComposioSignature(rawBody, webhookId, webhookTimestamp, sigHeader);
      if (!valid) {
        console.error(`[channel-webhook/composio] signature mismatch`);
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

  const triggerType = (body.type as string) ?? "";
  const triggerLower = triggerType.toLowerCase();
  const data = body.data as Record<string, unknown> | undefined;
  if (!data) {
    return jsonResponse({ ok: true, skipped: "no_data" });
  }

  // Extract trigger name from metadata.trigger_slug (Composio's field name)
  const metadata = body.metadata as Record<string, unknown> | undefined;
  const triggerName = (
    (metadata?.trigger_slug as string) ?? (metadata?.trigger_name as string) ??
    (body.trigger_name as string) ?? ""
  ).toLowerCase();
  console.log(`[channel-webhook/composio] trigger_slug=${triggerName || "(empty)"} type=${triggerType}`);

  // Route by trigger_name first (specific), then fall back to type.
  // If trigger_name is still empty, infer from data keys.
  let routeKey = triggerName || triggerLower;
  if (!triggerName && data) {
    // Gmail events have message_id + message_text + sender
    if ("message_id" in data && "message_text" in data) {
      routeKey = "googlesuper_new_message";
    }
    // Outlook events have event_type + id (minimal payload, need Graph API fetch)
    else if ("event_type" in data && !("message_text" in data)) {
      routeKey = "outlook_message_trigger";
    }
  }

  if (SLACK_TRIGGERS.includes(routeKey)) {
    if (!(await isChannelEnabled("slack"))) {
      return jsonResponse({ ok: true, skipped: "channel_disabled", platform: "slack" });
    }
    return handleSlackTrigger(data);
  }
  if (routeKey === "gmail_new_gmail_message" || routeKey === "googlesuper_new_message") {
    if (!(await isChannelEnabled("gmail"))) {
      return jsonResponse({ ok: true, skipped: "channel_disabled", platform: "gmail" });
    }
    return handleGmailTrigger(data);
  }
  if (routeKey === "outlook_message_trigger") {
    if (!(await isChannelEnabled("outlook"))) {
      return jsonResponse({ ok: true, skipped: "channel_disabled", platform: "outlook" });
    }
    return handleOutlookTrigger(data, metadata);
  }

  console.log(`[channel-webhook/composio] unhandled trigger type: ${triggerType}`);
  return jsonResponse({ ok: true, skipped: "unhandled_trigger", type: triggerType });
}

// ---------------------------------------------------------------------------
// Slack trigger handler (from Composio)
// ---------------------------------------------------------------------------

async function handleSlackTrigger(data: Record<string, unknown>): Promise<Response> {
  // V3 payload uses "text", V2 used "message"
  const messageText = (data.text as string) ?? (data.message as string) ?? "";
  if (!messageText) {
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
    "connection",
  );
}

// ---------------------------------------------------------------------------
// Gmail trigger handler (from Composio)
// ---------------------------------------------------------------------------

async function handleGmailTrigger(data: Record<string, unknown>): Promise<Response> {
  const messageId = (data.message_id as string) ?? "";
  const sender = (data.sender as string) ?? "";
  const subject = (data.subject as string) ?? "";
  const messageText = (data.message_text as string) ?? "";
  const attachmentList = data.attachment_list as Array<Record<string, unknown>> | undefined;

  // Extract to/cc/bcc from payload headers if available
  const payload = data.payload as Record<string, unknown> | undefined;
  const headers = (payload?.headers ?? []) as Array<{ name?: string; value?: string }>;
  const getHeader = (name: string) => headers.find((h) => h.name?.toLowerCase() === name)?.value ?? "";
  const to = getHeader("to");
  const cc = getHeader("cc");
  const bcc = getHeader("bcc");

  if (!messageText && !subject) {
    return jsonResponse({ ok: true, skipped: "empty_email" });
  }

  console.log(`[channel-webhook/gmail] from=${sender} subject="${subject}"`);

  // Build combined text with optional attachment list
  let combinedText = `Subject: ${subject}\n\n${messageText}`;
  if (Array.isArray(attachmentList) && attachmentList.length > 0) {
    const lines = attachmentList.map((a) => {
      const name = (a.filename as string) ?? (a.name as string) ?? "unnamed";
      const size = a.size as number | undefined;
      return size ? `- ${name} (${formatFileSize(size)})` : `- ${name}`;
    });
    combinedText += `\n\nAttachments:\n${lines.join("\n")}`;
  }

  await insertQueue({
    source: "email",
    priority: 3,
    buffer_key: `gmail:${messageId}`,
    combined_text: combinedText,
    metadata: {
      email_source: "gmail",
      message_id: messageId,
      sender,
      subject,
      to,
      cc,
      bcc,
    },
  });

  return jsonResponse({ ok: true, queued: true, source: "gmail" });
}

// ---------------------------------------------------------------------------
// Outlook trigger handler (from Composio)
// ---------------------------------------------------------------------------

async function handleOutlookTrigger(data: Record<string, unknown>, metadata?: Record<string, unknown>): Promise<Response> {
  const messageId = (data.id as string) ?? "";
  // connected_account_id is in metadata (from Composio webhook envelope)
  const connectionNanoId = (metadata?.connected_account_id as string) ?? (data.connection_nano_id as string) ?? "";

  console.log(`[channel-webhook/outlook] messageId=${messageId} connectionId=${connectionNanoId}`);

  if (!messageId || !connectionNanoId) {
    console.error(`[channel-webhook/outlook] missing_id — messageId=${messageId} connectionId=${connectionNanoId}`);
    return jsonResponse({ ok: true, skipped: "missing_id" });
  }

  // Fetch full email from Graph API via Composio token
  const accessToken = await fetchComposioAccessToken(connectionNanoId);
  if (!accessToken) {
    console.error("[channel-webhook/outlook] failed to get Composio access token");
    return jsonResponse({ ok: false, error: "token_fetch_failed" }, 500);
  }

  const email = await fetchOutlookEmail(messageId, accessToken);
  if (!email) {
    console.error("[channel-webhook/outlook] failed to fetch email content");
    return jsonResponse({ ok: false, error: "email_fetch_failed" }, 500);
  }

  console.log(`[channel-webhook/outlook] from=${email.from} subject="${email.subject}"`);

  // Build combined text with optional attachment list
  let combinedText = `Subject: ${email.subject}\n\n${email.body}`;
  if (email.attachments.length > 0) {
    const lines = email.attachments.map((a) =>
      a.size ? `- ${a.name} (${formatFileSize(a.size)})` : `- ${a.name}`
    );
    combinedText += `\n\nAttachments:\n${lines.join("\n")}`;
  }

  await insertQueue({
    source: "email",
    priority: 3,
    buffer_key: `outlook:${messageId}`,
    combined_text: combinedText,
    metadata: {
      email_source: "outlook",
      message_id: messageId,
      sender: email.from,
      subject: email.subject,
      to: email.to,
      cc: email.cc,
      bcc: email.bcc,
    },
  });

  return jsonResponse({ ok: true, queued: true, source: "outlook" });
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

  // Check if Slack channel is enabled
  if (!(await isChannelEnabled("slack"))) {
    return jsonResponse({ ok: true, skipped: "channel_disabled", platform: "slack" });
  }

  // Check if bot is configured (token exists in vault)
  const botTokenCheck = await getVaultSecret("slack_bot_token");
  if (!botTokenCheck) {
    return jsonResponse({ ok: true, skipped: "bot_not_configured" });
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
    channelType === "im" ? "chat_surface" : "connection",
  );
}

// ---------------------------------------------------------------------------
// Teams: Microsoft Graph change notifications
// ---------------------------------------------------------------------------

const TEAMS_DEBOUNCE_MS = 5_000;
// Microsoft Graph often sends 2-3 duplicate notifications per message,
// spaced ~3-4 seconds apart. DM debounce must exceed that gap.
const TEAMS_DM_DEBOUNCE_MS = 5_000;

let _teamsTokenCache: { token: string; connectionId: string; expiresAt: number } | null = null;

/**
 * Get the Microsoft access token via Composio connected account.
 * Connection ID is stored in vault as "teams_composio_connection_id".
 */
async function getTeamsAccessToken(): Promise<string | null> {
  // Return cached token if still valid (cache for 5 minutes)
  if (_teamsTokenCache && Date.now() < _teamsTokenCache.expiresAt) {
    return _teamsTokenCache.token;
  }

  // Look up connection ID from vault
  const connectionId = await getVaultSecret("teams_composio_connection_id");
  if (!connectionId) {
    console.error("[channel-webhook/teams] teams_composio_connection_id not in vault");
    return null;
  }

  const token = await fetchComposioAccessToken(connectionId);
  if (token) {
    _teamsTokenCache = { token, connectionId, expiresAt: Date.now() + 5 * 60 * 1000 };
  }
  return token;
}

let _teamsUserId: string | null = null;

/**
 * Get the connected user's Microsoft Teams user ID (from /me).
 * Cached for the lifetime of the edge function invocation.
 */
async function getConnectedTeamsUserId(accessToken: string): Promise<string | null> {
  if (_teamsUserId) return _teamsUserId;

  try {
    const resp = await fetch("https://graph.microsoft.com/v1.0/me", {
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
    });
    if (!resp.ok) return null;
    const user = await resp.json();
    _teamsUserId = user.id ?? null;
    console.log(`[channel-webhook/teams] connected user: ${user.displayName} (${_teamsUserId})`);
    return _teamsUserId;
  } catch {
    return null;
  }
}

/**
 * Parse resource string from Microsoft Graph change notification.
 * Formats:
 *   chats('{chatId}')/messages('{messageId}')
 *   chats/{chatId}/messages/{messageId}
 *   teams('{teamId}')/channels('{channelId}')/messages('{messageId}')
 *   teams/{teamId}/channels/{channelId}/messages/{messageId}
 */
function parseTeamsResource(resource: string): {
  chatId?: string;
  messageId?: string;
  teamId?: string;
  channelId?: string;
} | null {
  // chats('{id}')/messages('{id}')
  const chatMatch = resource.match(/chats\('([^']+)'\)\/messages\('([^']+)'\)/);
  if (chatMatch) return { chatId: chatMatch[1], messageId: chatMatch[2] };

  // chats/{id}/messages/{id}
  const chatAlt = resource.match(/chats\/([^/]+)\/messages\/([^/]+)/);
  if (chatAlt) return { chatId: chatAlt[1], messageId: chatAlt[2] };

  // teams('{id}')/channels('{id}')/messages('{id}')
  const channelMatch = resource.match(
    /teams\('([^']+)'\)\/channels\('([^']+)'\)\/messages\('([^']+)'\)/,
  );
  if (channelMatch) return { teamId: channelMatch[1], channelId: channelMatch[2], messageId: channelMatch[3] };

  // teams/{id}/channels/{id}/messages/{id}
  const channelAlt = resource.match(/teams\/([^/]+)\/channels\/([^/]+)\/messages\/([^/]+)/);
  if (channelAlt) return { teamId: channelAlt[1], channelId: channelAlt[2], messageId: channelAlt[3] };

  return null;
}

interface TeamsMessageResult {
  id: string;
  senderName: string;
  senderId: string;
  text: string;
  messageType: string;
  createdDateTime: string;
  mentions: Array<{ mentioned?: { user?: { id?: string } } }>;
}

/**
 * Fetch a Teams chat message from Microsoft Graph API.
 */
async function fetchTeamsChatMessage(
  chatId: string,
  messageId: string,
  accessToken: string,
): Promise<TeamsMessageResult | null> {
  try {
    const resp = await fetch(
      `https://graph.microsoft.com/v1.0/chats/${encodeURIComponent(chatId)}/messages/${encodeURIComponent(messageId)}`,
      { headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" } },
    );
    if (!resp.ok) {
      console.error(`[channel-webhook/teams] fetch chat message failed: ${resp.status}`);
      return null;
    }
    const msg = await resp.json();
    return parseGraphMessage(msg, chatId);
  } catch (e) {
    console.error(`[channel-webhook/teams] fetch chat message error:`, e);
    return null;
  }
}

/**
 * Fetch a Teams channel message from Microsoft Graph API.
 */
async function fetchTeamsChannelMessage(
  teamId: string,
  channelId: string,
  messageId: string,
  accessToken: string,
): Promise<TeamsMessageResult | null> {
  try {
    const resp = await fetch(
      `https://graph.microsoft.com/v1.0/teams/${encodeURIComponent(teamId)}/channels/${encodeURIComponent(channelId)}/messages/${encodeURIComponent(messageId)}`,
      { headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" } },
    );
    if (!resp.ok) {
      console.error(`[channel-webhook/teams] fetch channel message failed: ${resp.status}`);
      return null;
    }
    const msg = await resp.json();
    return parseGraphMessage(msg, `${teamId}:${channelId}`);
  } catch (e) {
    console.error(`[channel-webhook/teams] fetch channel message error:`, e);
    return null;
  }
}

function parseGraphMessage(msg: Record<string, unknown>, locationId: string): TeamsMessageResult {
  const from = msg.from as Record<string, unknown> | undefined;
  const user = (from?.user ?? {}) as Record<string, unknown>;
  const body = (msg.body ?? {}) as Record<string, unknown>;
  const content = (body.content as string) ?? "";
  const contentType = (body.contentType as string) ?? "text";

  return {
    id: (msg.id as string) ?? "",
    senderName: (user.displayName as string) ?? "Unknown",
    senderId: (user.id as string) ?? "",
    text: contentType === "html" ? stripHtmlTags(content) : content,
    messageType: (msg.messageType as string) ?? "message",
    createdDateTime: (msg.createdDateTime as string) ?? "",
    mentions: (msg.mentions ?? []) as TeamsMessageResult["mentions"],
  };
}

function stripHtmlTags(html: string): string {
  return html
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .trim();
}

/**
 * Handle Teams webhook — GET for validation, POST for change notifications.
 */
async function handleTeams(req: Request): Promise<Response> {
  const url = new URL(req.url);

  // GET: Microsoft Graph subscription validation
  if (req.method === "GET") {
    const validationToken = url.searchParams.get("validationToken");
    if (!validationToken) {
      return jsonResponse({ error: "Missing validationToken" }, 400);
    }
    console.log("[channel-webhook/teams] validation request, returning token");
    return new Response(validationToken, {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    });
  }

  // Check if Teams channel is enabled (after validation — always allow validation)
  if (!(await isChannelEnabled("teams"))) {
    return jsonResponse({ ok: true, skipped: "channel_disabled", platform: "teams" });
  }

  // POST: Change notifications
  const rawBody = await req.text();

  // Check for validation token in POST (Microsoft sometimes sends as POST)
  const validationToken = url.searchParams.get("validationToken");
  if (validationToken) {
    return new Response(validationToken, {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    });
  }

  let body: Record<string, unknown>;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const notifications = (body.value ?? []) as Array<Record<string, unknown>>;
  if (notifications.length === 0) {
    return jsonResponse({ ok: true, skipped: "no_notifications" });
  }

  // Verify clientState from vault
  const webhookSecret = await getVaultSecret("teams_webhook_secret");

  // Get access token for fetching messages
  const accessToken = await getTeamsAccessToken();
  if (!accessToken) {
    console.error("[channel-webhook/teams] no access token available");
    return jsonResponse({ ok: false, error: "token_unavailable" }, 500);
  }

  // Get connected user ID for mention filtering in channels
  const connectedUserId = await getConnectedTeamsUserId(accessToken);

  let processed = 0;
  let skipped = 0;

  for (const notification of notifications) {
    // Validate clientState
    if (webhookSecret && notification.clientState !== webhookSecret) {
      console.warn("[channel-webhook/teams] invalid clientState, skipping");
      skipped++;
      continue;
    }

    // Only process "created" messages
    if (notification.changeType !== "created") {
      skipped++;
      continue;
    }

    const resource = (notification.resource as string) ?? "";
    const resourceInfo = parseTeamsResource(resource);
    if (!resourceInfo || !resourceInfo.messageId) {
      console.error(`[channel-webhook/teams] failed to parse resource: ${resource}`);
      skipped++;
      continue;
    }

    const { chatId, messageId, teamId, channelId } = resourceInfo;
    const isChannelMessage = !!teamId && !!channelId;

    // Fetch the full message
    let message: TeamsMessageResult | null;
    if (isChannelMessage) {
      message = await fetchTeamsChannelMessage(teamId!, channelId!, messageId!, accessToken);
    } else {
      message = await fetchTeamsChatMessage(chatId!, messageId!, accessToken);
    }

    if (!message) {
      console.error(`[channel-webhook/teams] failed to fetch message ${messageId}`);
      skipped++;
      continue;
    }

    // Skip system messages
    if (message.messageType !== "message") {
      skipped++;
      continue;
    }

    // Skip own messages
    if (connectedUserId && message.senderId === connectedUserId) {
      skipped++;
      continue;
    }

    // Skip empty messages
    if (!message.text.trim()) {
      skipped++;
      continue;
    }

    // For channel messages: only process if connected user is @mentioned
    if (isChannelMessage && connectedUserId) {
      const isMentioned = message.mentions.some(
        (m) => m.mentioned?.user?.id === connectedUserId,
      );
      if (!isMentioned) {
        skipped++;
        continue;
      }
    }

    console.log(
      `[channel-webhook/teams] ${isChannelMessage ? "channel" : "chat"} message from ${message.senderName}: ${message.text.substring(0, 80)}`,
    );

    // Buffer key: teams:{chatId} or teams:{teamId}:{channelId}
    const locationId = isChannelMessage ? `${teamId}:${channelId}` : chatId!;
    const bufferKey = `teams:${locationId}`;
    const chatType = isChannelMessage ? "channel" : "chat";
    const priority = chatType === "chat" ? 1 : 2;
    const debounceMs = chatType === "chat" ? TEAMS_DM_DEBOUNCE_MS : TEAMS_DEBOUNCE_MS;

    // Insert into buffer (reuse message_ts metadata key for dedup compatibility)
    const inserted = await insertBuffer({
      source: "teams",
      buffer_key: bufferKey,
      sender: message.senderId,
      sender_name: message.senderName,
      message_text: message.text,
      metadata: {
        message_ts: message.id, // Teams message ID in message_ts for dedup index
        message_id: message.id,
        chat_id: isChannelMessage ? undefined : chatId,
        team_id: isChannelMessage ? teamId : undefined,
        channel_id: isChannelMessage ? channelId : undefined,
        chat_type: chatType,
        priority,
        created_date_time: message.createdDateTime,
      },
    });

    if (!inserted) {
      skipped++;
      continue;
    }

    const insertedAt = inserted.created_at as string;

    // Debounce — wait, then check if newer messages arrived
    await new Promise((r) => setTimeout(r, debounceMs));

    const hasNewer = await checkNewerMessages(bufferKey, insertedAt);
    if (hasNewer) {
      console.log(`[channel-webhook/teams] ${bufferKey} debounce: newer messages, skipping flush`);
      processed++;
      continue;
    }

    // Flush — atomically grab all buffered messages
    const flushedRows = await flushBuffer(bufferKey);
    if (flushedRows.length === 0) {
      processed++;
      continue;
    }

    // Combine into batch and insert into dispatch queue
    const senders = new Set<string>();
    const senderIds = new Set<string>();
    const lines: string[] = [];

    for (const row of flushedRows) {
      const name = (row.sender_name as string) || (row.sender as string);
      senders.add(name);
      if (row.sender) senderIds.add(row.sender as string);
      lines.push(`[${name}] ${row.message_text}`);
    }

    const combinedText = lines.join("\n");

    await insertQueue({
      source: "teams",
      priority,
      buffer_key: bufferKey,
      combined_text: combinedText,
      metadata: {
        chat_id: isChannelMessage ? undefined : chatId,
        team_id: isChannelMessage ? teamId : undefined,
        channel_id: isChannelMessage ? channelId : undefined,
        chat_type: chatType,
        via: "connection",
        senders: [...senders],
        sender_ids: [...senderIds],
        message_count: flushedRows.length,
      },
    });

    console.log(`[channel-webhook/teams] ${bufferKey} flushed ${flushedRows.length} messages to queue`);
    processed++;
  }

  return jsonResponse({ ok: true, processed, skipped });
}

// ---------------------------------------------------------------------------
// Meeting transcript handler (from meeting-recorder app)
// ---------------------------------------------------------------------------

async function handleMeetings(req: Request): Promise<Response> {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  const title = (body.title as string) ?? "Untitled Meeting";
  const transcript = (body.transcript as string) ?? "";
  const transcriptFilename = (body.transcriptFilename as string) ?? "";
  const duration = body.duration as number | undefined;
  const startedAt = (body.startedAt as string) ?? "";
  const source = (body.source as string) ?? "";
  const meetingPlatform = (body.meetingPlatform as string) ?? "";
  const calendarSource = (body.calendarSource as string) ?? "";
  const calendarEventId = (body.calendarEventId as string) ?? "";
  const attendees = body.attendees as Array<Record<string, unknown>> | undefined;

  if (!transcript) {
    return jsonResponse({ ok: true, skipped: "empty_transcript" });
  }

  // Format attendees with name, email, and status
  const attendeeLines = attendees
    ? attendees.map((a) => {
        const name = (a.name as string) ?? "Unknown";
        const email = (a.email as string) ?? "";
        const status = (a.status as string) ?? "";
        const parts = [name];
        if (email) parts.push(`<${email}>`);
        if (status) parts.push(`(${status})`);
        return parts.join(" ");
      })
    : [];

  // Format duration
  const durationStr = duration
    ? `${Math.floor(duration / 60)}m ${duration % 60}s`
    : "";

  // Build combined text with metadata header + transcript
  const metaLines = [`Meeting: ${title}`];
  if (meetingPlatform) metaLines.push(`Platform: ${meetingPlatform}`);
  if (startedAt) metaLines.push(`Started: ${startedAt}`);
  if (durationStr) metaLines.push(`Duration: ${durationStr}`);
  if (source) metaLines.push(`Trigger: ${source}`);
  if (calendarSource) metaLines.push(`Calendar: ${calendarSource}`);
  if (attendeeLines.length) metaLines.push(`Attendees:\n${attendeeLines.map((l) => `  - ${l}`).join("\n")}`);
  if (transcriptFilename) metaLines.push(`Transcript file: /mnt/meeting-transcripts/${transcriptFilename}`);
  metaLines.push("", transcript);

  const combinedText = metaLines.join("\n");

  // Insert directly into queue (no debounce needed for meetings)
  await insertQueue({
    source: "meeting",
    priority: 3,
    buffer_key: `meeting:${body.id ?? Date.now()}`,
    combined_text: combinedText,
    metadata: {
      title,
      trigger: source,
      transcript_filename: transcriptFilename,
      meeting_platform: meetingPlatform,
      calendar_source: calendarSource,
      calendar_event_id: calendarEventId,
      started_at: startedAt,
      duration,
      attendees: attendeeLines,
    },
  });

  console.log(`[channel-webhook/meetings] queued: ${title}`);
  return jsonResponse({ ok: true, queued: true, title });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function escapeXml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

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
  const url = new URL(req.url);
  const path = url.pathname;

  // Teams accepts GET (validation) and POST (notifications)
  if (path.endsWith("/teams")) {
    if (req.method !== "GET" && req.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }
    return handleTeams(req);
  }

  // All other routes are POST only
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  if (path.endsWith("/slack-bot")) {
    return handleSlackBot(req);
  }
  if (path.endsWith("/composio") || path.endsWith("/slack")) {
    return handleComposio(req);
  }
  if (path.endsWith("/meetings")) {
    return handleMeetings(req);
  }

  return jsonResponse({ error: "Unknown route" }, 404);
});

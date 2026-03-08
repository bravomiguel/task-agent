/**
 * channel-webhook — Supabase Edge Function
 *
 * Receives inbound Slack messages via Composio webhook triggers and buffers
 * them in the `inbound_buffer` table with a 5-second debounce window.
 * Messages from the same channel are grouped together. After the debounce
 * window closes, the batch is flushed to `inbound_queue` for the
 * queue-dispatcher to pick up.
 *
 * Route:
 *   POST /channel-webhook/slack — Composio SLACK_NEW_MESSAGE trigger
 *
 * Required env vars:
 *   COMPOSIO_WEBHOOK_SECRET — HMAC secret for Composio signature verification
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const COMPOSIO_WEBHOOK_SECRET = Deno.env.get("COMPOSIO_WEBHOOK_SECRET") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const DEBOUNCE_MS = 5_000;

// ---------------------------------------------------------------------------
// Composio HMAC verification
// ---------------------------------------------------------------------------

async function verifyComposioSignature(
  rawBody: string,
  signatureHeader: string,
): Promise<boolean> {
  if (!COMPOSIO_WEBHOOK_SECRET || !signatureHeader) return false;

  // Format: "v1,{base64}"
  const parts = signatureHeader.split(",");
  if (parts.length !== 2 || parts[0] !== "v1") return false;
  const expectedSig = parts[1];

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
    new TextEncoder().encode(rawBody),
  );
  const computedSig = btoa(String.fromCharCode(...new Uint8Array(sig)));

  return computedSig === expectedSig;
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

// ---------------------------------------------------------------------------
// Slack handler (Composio webhook format)
// ---------------------------------------------------------------------------

async function handleSlack(req: Request): Promise<Response> {
  const rawBody = await req.text();

  // Verify Composio HMAC signature
  if (COMPOSIO_WEBHOOK_SECRET) {
    const sigHeader = req.headers.get("webhook-signature") ?? "";
    const valid = await verifyComposioSignature(rawBody, sigHeader);
    if (!valid) {
      return new Response("Invalid signature", { status: 401 });
    }
  }

  let body: Record<string, unknown>;
  try {
    body = JSON.parse(rawBody);
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  // Extract from Composio trigger payload
  const data = body.data as Record<string, unknown> | undefined;
  if (!data) {
    console.log("[channel-webhook] No data in payload, skipping");
    return jsonResponse({ ok: true, skipped: "no_data" });
  }

  const messageText = (data.message as string) ?? "";
  if (!messageText) {
    return jsonResponse({ ok: true, skipped: "empty_message" });
  }

  const senderObj = data.sender as Record<string, unknown> | undefined;
  const senderId = (senderObj?.id as string) ?? "unknown";
  const senderName = (senderObj?.name as string) ?? senderId;
  const channelId = (data.channel as string) ?? (data.channel_id as string) ?? "unknown";
  const channelType = (data.channel_type as string) ?? "";
  const messageTs = String(data.timestamp ?? "");
  const threadTs = (data.thread_ts as string) ?? "";

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
    // Duplicate or insert error — skip
    return jsonResponse({ ok: true, skipped: "duplicate_or_error" });
  }

  const insertedAt = inserted.created_at as string;

  // Step 2: Debounce — wait, then check if newer messages arrived
  await new Promise((r) => setTimeout(r, DEBOUNCE_MS));

  const hasNewer = await checkNewerMessages(bufferKey, insertedAt);
  if (hasNewer) {
    // A later invocation will handle the flush
    console.log(`[channel-webhook] ${bufferKey} debounce: newer messages exist, skipping flush`);
    return jsonResponse({ ok: true, debounced: true });
  }

  // Step 3: Flush — atomically grab all buffered messages for this key
  const flushedRows = await flushBuffer(bufferKey);
  if (flushedRows.length === 0) {
    // Another invocation already flushed (race condition — safe to skip)
    return jsonResponse({ ok: true, skipped: "already_flushed" });
  }

  // Step 4: Combine into a single batch and insert into dispatch queue
  const senders = new Set<string>();
  const lines: string[] = [];
  let lastThreadTs = "";

  for (const row of flushedRows) {
    const name = (row.sender_name as string) || (row.sender as string);
    senders.add(name);
    lines.push(`[${name}] ${row.message_text}`);
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
      message_count: flushedRows.length,
    },
  });

  console.log(`[channel-webhook] ${bufferKey} flushed ${flushedRows.length} messages to queue`);
  return jsonResponse({ ok: true, flushed: true, count: flushedRows.length });
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

  if (path.endsWith("/slack")) {
    return handleSlack(req);
  }

  return jsonResponse({ error: "Unknown platform. Use /slack" }, 404);
});

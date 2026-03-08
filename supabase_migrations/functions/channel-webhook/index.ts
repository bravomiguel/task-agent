/**
 * channel-webhook — Supabase Edge Function
 *
 * Receives inbound messages from chat platforms (Slack, Teams) and creates
 * LangGraph runs so the agent can process and reply. Each platform has its
 * own verification and payload parsing, but they share the same two-step
 * LangGraph flow: create thread → start run.
 *
 * Routes:
 *   POST /channel-webhook/slack   — Slack Events API
 *   POST /channel-webhook/teams   — Microsoft Teams Bot Framework
 *
 * The agent's response is sent back via the send_message tool (outbound),
 * not by this function. This function is fire-and-forget.
 *
 * Required env vars:
 *   LANGGRAPH_API_URL — base URL of the LangGraph server
 *   SLACK_SIGNING_SECRET — for Slack request verification
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const LANGGRAPH_API_URL = Deno.env.get("LANGGRAPH_API_URL") ?? "";
const SLACK_SIGNING_SECRET = Deno.env.get("SLACK_SIGNING_SECRET") ?? "";

// ---------------------------------------------------------------------------
// Slack verification
// ---------------------------------------------------------------------------

async function verifySlackSignature(
  req: Request,
  rawBody: string,
): Promise<boolean> {
  if (!SLACK_SIGNING_SECRET) return false;

  const timestamp = req.headers.get("x-slack-request-timestamp") ?? "";
  const signature = req.headers.get("x-slack-signature") ?? "";

  // Reject requests older than 5 minutes
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - Number(timestamp)) > 300) return false;

  const sigBasestring = `v0:${timestamp}:${rawBody}`;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(SLACK_SIGNING_SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(sigBasestring),
  );
  const hex = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  return signature === `v0=${hex}`;
}

// ---------------------------------------------------------------------------
// LangGraph helpers (same pattern as cron-launcher)
// ---------------------------------------------------------------------------

async function createThreadAndRun(
  platform: string,
  sender: string,
  channel: string,
  text: string,
  metadata: Record<string, unknown> = {},
): Promise<{ ok: boolean; thread_id?: string; error?: string }> {
  if (!LANGGRAPH_API_URL) {
    return { ok: false, error: "LANGGRAPH_API_URL not configured" };
  }

  const headers = { "Content-Type": "application/json" };

  // Step 1: create thread
  let threadId: string;
  try {
    const r1 = await fetch(`${LANGGRAPH_API_URL}/threads`, {
      method: "POST",
      headers,
      body: JSON.stringify({}),
    });
    if (!r1.ok) {
      const t = await r1.text();
      console.error(`[channel-webhook] create thread failed ${r1.status}: ${t}`);
      return { ok: false, error: `Create thread failed: ${r1.status}` };
    }
    threadId = (await r1.json()).thread_id;
  } catch (err) {
    console.error("[channel-webhook] create thread error:", err);
    return { ok: false, error: `Create thread error: ${err}` };
  }

  // Build system-message tag with channel context
  const attrs = [
    `type="channel-message"`,
    `platform="${platform}"`,
    `sender="${sender}"`,
    `channel="${channel}"`,
  ];
  for (const [k, v] of Object.entries(metadata)) {
    if (v != null) attrs.push(`${k}="${v}"`);
  }
  const message = `<system-message ${attrs.join(" ")}>\n${text}\n</system-message>`;

  // Step 2: start run
  try {
    const r2 = await fetch(`${LANGGRAPH_API_URL}/threads/${threadId}/runs`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        assistant_id: "main",
        input: {
          messages: [{ role: "user", content: message }],
          session_type: "channel",
          channel_platform: platform,
          channel_sender: sender,
          channel_id: channel,
          channel_metadata: metadata,
        },
        stream_resumable: true,
      }),
    });
    if (!r2.ok) {
      const t = await r2.text();
      console.error(`[channel-webhook] start run failed ${r2.status}: ${t}`);
      return { ok: false, error: `Start run failed: ${r2.status}` };
    }
  } catch (err) {
    console.error("[channel-webhook] start run error:", err);
    return { ok: false, error: `Start run error: ${err}` };
  }

  console.log(`[channel-webhook] ${platform} sender=${sender} channel=${channel} thread=${threadId}`);
  return { ok: true, thread_id: threadId };
}

// ---------------------------------------------------------------------------
// Slack handler
// ---------------------------------------------------------------------------

async function handleSlack(req: Request): Promise<Response> {
  const rawBody = await req.text();

  // Verify signature
  if (SLACK_SIGNING_SECRET) {
    const valid = await verifySlackSignature(req, rawBody);
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

  // Handle Slack URL verification challenge
  if (body.type === "url_verification") {
    return new Response(JSON.stringify({ challenge: body.challenge }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Handle event callbacks
  if (body.type !== "event_callback") {
    return new Response("OK", { status: 200 });
  }

  const event = body.event as Record<string, unknown> | undefined;
  if (!event) {
    return new Response("OK", { status: 200 });
  }

  // Only process messages (not bot messages, not edits, not deletions)
  if (
    event.type !== "message" ||
    event.subtype != null ||
    event.bot_id != null
  ) {
    return new Response("OK", { status: 200 });
  }

  const text = (event.text as string) ?? "";
  const sender = (event.user as string) ?? "unknown";
  const channel = (event.channel as string) ?? "unknown";
  const threadTs = event.thread_ts as string | undefined;
  const ts = event.ts as string | undefined;
  const teamId = (body.team_id as string) ?? "";

  const result = await createThreadAndRun("slack", sender, channel, text, {
    thread_ts: threadTs ?? ts,
    team_id: teamId,
  });

  // Always return 200 to Slack to avoid retries
  return new Response(JSON.stringify(result), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// ---------------------------------------------------------------------------
// Teams handler
// ---------------------------------------------------------------------------

async function handleTeams(req: Request): Promise<Response> {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return new Response("Invalid JSON", { status: 400 });
  }

  // Teams Bot Framework sends activities
  const activityType = body.type as string | undefined;
  if (activityType !== "message") {
    // Respond 200 to non-message activities (e.g. conversationUpdate)
    return new Response("OK", { status: 200 });
  }

  const text = (body.text as string) ?? "";
  const from = body.from as Record<string, unknown> | undefined;
  const sender = (from?.aadObjectId as string) ?? (from?.id as string) ?? "unknown";
  const senderName = (from?.name as string) ?? "";
  const conversation = body.conversation as Record<string, unknown> | undefined;
  const conversationId = (conversation?.id as string) ?? "unknown";
  const conversationType = (conversation?.conversationType as string) ?? "";
  const channelId = (body.channelId as string) ?? "";
  const serviceUrl = (body.serviceUrl as string) ?? "";
  const activityId = (body.id as string) ?? "";
  const tenantId = ((body.channelData as Record<string, unknown>)?.tenant as Record<string, unknown>)?.id as string ?? "";

  // Strip bot @mention from message text (Teams includes it)
  const cleanText = text.replace(/<at>.*?<\/at>\s*/g, "").trim();
  if (!cleanText) {
    return new Response("OK", { status: 200 });
  }

  const result = await createThreadAndRun("teams", sender, conversationId, cleanText, {
    sender_name: senderName,
    conversation_type: conversationType,
    channel_id: channelId,
    service_url: serviceUrl,
    activity_id: activityId,
    tenant_id: tenantId,
  });

  return new Response(JSON.stringify(result), {
    status: 200,
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

  // Route by path suffix: /channel-webhook/slack, /channel-webhook/teams
  if (path.endsWith("/slack")) {
    return handleSlack(req);
  }
  if (path.endsWith("/teams")) {
    return handleTeams(req);
  }

  return new Response(
    JSON.stringify({ error: "Unknown platform. Use /slack or /teams" }),
    { status: 404, headers: { "Content-Type": "application/json" } },
  );
});

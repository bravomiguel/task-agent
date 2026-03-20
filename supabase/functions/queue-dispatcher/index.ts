/**
 * queue-dispatcher — Supabase Edge Function
 *
 * Called by pg_cron every 10 seconds. Drains per-thread queues: for each
 * thread that has pending items in `inbound_queue`, checks if the thread is
 * idle, and dispatches the oldest item if so.
 *
 * Also handles items without a thread_id (e.g. Slack messages that need
 * routing to the latest main thread).
 *
 * Required env vars:
 *   LANGGRAPH_API_URL — base URL of the LangGraph server
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const LANGGRAPH_API_URL = Deno.env.get("LANGGRAPH_API_URL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

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

/**
 * Fetch all pending items grouped by thread, oldest first per thread.
 * Returns items with distinct thread_ids (one per thread, highest priority/oldest).
 */
async function fetchPendingItems(): Promise<Record<string, unknown>[]> {
  // Get all undispatched items, ordered by priority then created_at
  const params = new URLSearchParams({
    dispatched_at: "is.null",
    order: "priority.asc,created_at.asc",
    limit: "50",
  });
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_queue?${params}`, {
    headers: supabaseHeaders(),
  });
  if (!resp.ok) {
    console.error(`[queue-dispatcher] fetch pending failed: ${resp.status}`);
    return [];
  }
  return await resp.json();
}

async function markDispatched(id: string): Promise<void> {
  const resp = await fetch(
    `${SUPABASE_URL}/rest/v1/inbound_queue?id=eq.${id}`,
    {
      method: "PATCH",
      headers: supabaseHeaders(),
      body: JSON.stringify({ dispatched_at: new Date().toISOString() }),
    },
  );
  if (!resp.ok) {
    console.error(`[queue-dispatcher] mark dispatched failed: ${resp.status}`);
  }
}

// ---------------------------------------------------------------------------
// LangGraph helpers
// ---------------------------------------------------------------------------

const lgHeaders = { "Content-Type": "application/json" };

async function getThreadStatus(threadId: string): Promise<string | null> {
  const resp = await fetch(`${LANGGRAPH_API_URL}/threads/${threadId}`, {
    headers: lgHeaders,
  });
  if (!resp.ok) return null;
  const data = await resp.json();
  return data.status ?? null;
}

async function findMainThread(): Promise<Record<string, unknown> | null> {
  const resp = await fetch(`${LANGGRAPH_API_URL}/threads/search`, {
    method: "POST",
    headers: lgHeaders,
    body: JSON.stringify({
      limit: 10,
      sort_by: "updated_at",
      sort_order: "desc",
    }),
  });
  if (!resp.ok) {
    console.error(`[queue-dispatcher] thread search failed: ${resp.status}`);
    return null;
  }
  const threads = await resp.json();
  for (const t of threads) {
    const values = t.values || {};
    if (values.session_type === "main") {
      return t;
    }
  }
  return null;
}

async function createThread(): Promise<string | null> {
  const resp = await fetch(`${LANGGRAPH_API_URL}/threads`, {
    method: "POST",
    headers: lgHeaders,
    body: JSON.stringify({}),
  });
  if (!resp.ok) {
    console.error(`[queue-dispatcher] create thread failed: ${resp.status}`);
    return null;
  }
  const data = await resp.json();
  return data.thread_id;
}

async function startRun(
  threadId: string,
  message: string,
  input: Record<string, unknown>,
): Promise<boolean> {
  const resp = await fetch(`${LANGGRAPH_API_URL}/threads/${threadId}/runs`, {
    method: "POST",
    headers: lgHeaders,
    body: JSON.stringify({
      assistant_id: "main",
      input: {
        messages: [{ role: "user", content: message }],
        ...input,
      },
      stream_resumable: true,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[queue-dispatcher] start run failed ${resp.status}: ${text}`);
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Dispatch logic
// ---------------------------------------------------------------------------

async function dispatchItem(item: Record<string, unknown>): Promise<{ dispatched: boolean; threadId?: string }> {
  const meta = (item.metadata || {}) as Record<string, unknown>;
  const source = (item.source as string) ?? "unknown";
  const combinedText = (item.combined_text as string) ?? "";
  const itemThreadId = item.thread_id as string | null;

  let threadId: string;
  let message: string;
  const runInput: Record<string, unknown> = { session_type: "main" };

  if (itemThreadId) {
    // Item has a specific target thread — check if it's idle
    const status = await getThreadStatus(itemThreadId);
    if (status === "busy") {
      return { dispatched: false };
    }
    threadId = itemThreadId;
  } else {
    // No target thread (e.g. Slack) — find the latest idle main thread
    const mainThread = await findMainThread();
    if (mainThread && mainThread.status === "busy") {
      return { dispatched: false };
    }
    if (mainThread) {
      threadId = mainThread.thread_id as string;
    } else {
      const newId = await createThread();
      if (!newId) return { dispatched: false };
      threadId = newId;
    }
  }

  // Build message based on source
  const via = (meta.via as string) ?? "";

  if (source === "slack") {
    const channelId = (meta.channel_id as string) ?? "";
    const threadTs = (meta.thread_ts as string) ?? "";
    const attrs = [
      `type="message"`,
      `platform="slack"`,
      `via="${via || "connection"}"`,
      `channel="${channelId}"`,
    ];
    if (threadTs) attrs.push(`thread_ts="${threadTs}"`);
    const channelType = (meta.channel_type as string) ?? "";
    const isDm = channelType === "im";
    const ids = meta.sender_ids as string[] | undefined;
    const names = meta.senders as string[] | undefined;
    if (names) attrs.push(`${isDm ? "sender" : "senders"}="${names.join(",")}"`);
    if (ids) attrs.push(`${isDm ? "sender_id" : "sender_ids"}="${ids.join(",")}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_platform = "slack";
    runInput.channel_id = channelId;
    runInput.channel_metadata = meta;
  } else if (source === "teams") {
    const chatId = (meta.chat_id as string) ?? "";
    const teamId = (meta.team_id as string) ?? "";
    const channelId = (meta.channel_id as string) ?? "";
    const chatType = (meta.chat_type as string) ?? "chat";
    const isChannel = chatType === "channel";
    const attrs = [
      `type="message"`,
      `platform="teams"`,
      `via="${via || "connection"}"`,
    ];
    if (isChannel) {
      attrs.push(`team_id="${teamId}"`);
      attrs.push(`channel_id="${channelId}"`);
    } else {
      attrs.push(`chat_id="${chatId}"`);
    }
    attrs.push(`chat_type="${chatType}"`);
    const ids = meta.sender_ids as string[] | undefined;
    const names = meta.senders as string[] | undefined;
    const isDm = chatType === "chat" || chatType === "bot-dm";
    if (names) attrs.push(`${isDm ? "sender" : "senders"}="${names.join(",")}"`);
    if (ids) attrs.push(`${isDm ? "sender_id" : "sender_ids"}="${ids.join(",")}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_platform = "teams";
    runInput.channel_id = isChannel ? `team:${teamId}/channel:${channelId}` : chatId;
    runInput.channel_metadata = meta;
  } else if (source === "telegram") {
    const chatId = (meta.chat_id as string) ?? "";
    const attrs = [
      `type="message"`,
      `platform="telegram"`,
      `via="direct_chat"`,
      `chat_id="${chatId}"`,
    ];
    const ids = meta.sender_ids as string[] | undefined;
    const names = meta.senders as string[] | undefined;
    if (names) attrs.push(`sender="${names[0]}"`);
    if (ids) attrs.push(`sender_id="${ids[0]}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_platform = "telegram";
    runInput.channel_id = chatId;
    runInput.channel_metadata = meta;
  } else if (source === "whatsapp") {
    const chatId = (meta.chat_id as string) ?? "";
    const attrs = [
      `type="message"`,
      `platform="whatsapp"`,
      `via="direct_chat"`,
      `chat_id="${chatId}"`,
    ];
    const ids = meta.sender_ids as string[] | undefined;
    const names = meta.senders as string[] | undefined;
    if (names) attrs.push(`sender="${names[0]}"`);
    if (ids) attrs.push(`sender_id="${ids[0]}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_platform = "whatsapp";
    runInput.channel_id = chatId;
    runInput.channel_metadata = meta;
  } else if (source === "email") {
    const emailSource = (meta.email_source as string) ?? "email";
    const sender = (meta.sender as string) ?? "";
    const subject = (meta.subject as string) ?? "";
    const messageId = (meta.message_id as string) ?? "";
    const to = (meta.to as string) ?? "";
    const cc = (meta.cc as string) ?? "";
    const bcc = (meta.bcc as string) ?? "";
    const attrs = [
      `type="email"`,
      `platform="${emailSource}"`,
    ];
    if (sender) attrs.push(`from="${sender}"`);
    if (subject) attrs.push(`subject="${subject}"`);
    if (messageId) attrs.push(`message_id="${messageId}"`);
    if (to) attrs.push(`to="${to}"`);
    if (cc) attrs.push(`cc="${cc}"`);
    if (bcc) attrs.push(`bcc="${bcc}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_platform = emailSource;
    runInput.channel_metadata = meta;
  } else if (source === "meeting") {
    const transcriptFilename = (meta.transcript_filename as string) ?? "";
    const attrs = [`type="meeting-transcript"`];
    if (transcriptFilename) attrs.push(`transcript_path="/mnt/meeting-transcripts/${transcriptFilename}"`);
    message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;
    runInput.channel_metadata = meta;
  } else {
    // Cron, heartbeat, subagent, sessions-send — combined_text is pre-formatted
    message = combinedText;
    if (meta.job_id != null) runInput.cron_job_id = meta.job_id;
    if (meta.job_name) runInput.cron_job_name = meta.job_name;
    if (meta.schedule_type) runInput.cron_schedule_type = meta.schedule_type;
  }

  const ok = await startRun(threadId, message, runInput);
  return { dispatched: ok, threadId: ok ? threadId : undefined };
}

async function dispatch(): Promise<Record<string, unknown>> {
  if (!LANGGRAPH_API_URL) {
    return { ok: false, error: "LANGGRAPH_API_URL not configured" };
  }

  const items = await fetchPendingItems();
  if (items.length === 0) {
    return { ok: true, idle: true };
  }

  // Group by thread — dispatch one item per thread per cycle
  const seenThreads = new Set<string>();
  let dispatched = 0;
  let skipped = 0;

  for (const item of items) {
    const threadKey = (item.thread_id as string) ?? "__unrouted__";
    if (seenThreads.has(threadKey)) continue;
    seenThreads.add(threadKey);

    const result = await dispatchItem(item);
    if (result.dispatched) {
      await markDispatched(item.id as string);
      dispatched++;
      console.log(`[queue-dispatcher] dispatched queue_id=${item.id} to thread=${result.threadId}`);
    } else {
      skipped++;
    }
  }

  return { ok: true, dispatched, skipped };
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  try {
    const result = await dispatch();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err) {
    console.error("[queue-dispatcher] error:", err);
    return new Response(JSON.stringify({ ok: false, error: String(err) }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
});

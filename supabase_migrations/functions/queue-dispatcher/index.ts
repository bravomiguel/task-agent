/**
 * queue-dispatcher — Supabase Edge Function
 *
 * Called by pg_cron every 10 seconds. Pops the oldest undispatched item from
 * `inbound_queue` and dispatches it to the agent's most recent idle main
 * session thread. If the main thread is busy, skips until the next poll.
 *
 * Dispatches ONE item per invocation to avoid overwhelming the agent.
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

async function fetchOldestPending(): Promise<Record<string, unknown> | null> {
  const params = new URLSearchParams({
    dispatched_at: "is.null",
    order: "priority.asc,created_at.asc",
    limit: "1",
  });
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_queue?${params}`, {
    headers: supabaseHeaders(),
  });
  if (!resp.ok) {
    console.error(`[queue-dispatcher] fetch pending failed: ${resp.status}`);
    return null;
  }
  const rows = await resp.json();
  return rows.length > 0 ? rows[0] : null;
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

/**
 * Find the most recent main session thread.
 * Returns the thread object or null if none found.
 */
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

/**
 * Create a new LangGraph thread.
 */
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

/**
 * Start a run on a thread with the given message and input state.
 */
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
// Main dispatch logic
// ---------------------------------------------------------------------------

async function dispatch(): Promise<Record<string, unknown>> {
  if (!LANGGRAPH_API_URL) {
    return { ok: false, error: "LANGGRAPH_API_URL not configured" };
  }

  // 1. Pop oldest undispatched item
  const item = await fetchOldestPending();
  if (!item) {
    return { ok: true, idle: true };
  }

  const itemId = item.id as string;
  const meta = (item.metadata || {}) as Record<string, unknown>;
  const source = (item.source as string) ?? "unknown";

  // 2. Find most recent main thread and check if idle
  const mainThread = await findMainThread();

  if (mainThread && mainThread.status === "busy") {
    console.log("[queue-dispatcher] main thread busy, skipping");
    return { ok: true, skipped: "main_thread_busy" };
  }

  // 3. Determine thread — use existing idle main or create new
  let threadId: string;
  let sessionType: string;

  if (mainThread && mainThread.status !== "busy") {
    // Post to existing idle main thread
    threadId = mainThread.thread_id as string;
    sessionType = "main";
  } else {
    // No main thread found — create a new one
    const newId = await createThread();
    if (!newId) {
      return { ok: false, error: "Failed to create thread" };
    }
    threadId = newId;
    sessionType = "main";
  }

  // 4. Build system-message with channel context
  const combinedText = (item.combined_text as string) ?? "";
  const channelId = (meta.channel_id as string) ?? "";
  const threadTs = (meta.thread_ts as string) ?? "";
  const platform = source === "slack" ? "slack" : source;

  const attrs = [
    `type="channel-message"`,
    `platform="${platform}"`,
    `channel="${channelId}"`,
  ];
  if (threadTs) attrs.push(`thread_ts="${threadTs}"`);
  if (meta.senders) attrs.push(`senders="${(meta.senders as string[]).join(",")}"`);

  const message = `<system-message ${attrs.join(" ")}>\n${combinedText}\n</system-message>`;

  // 5. Start run
  const ok = await startRun(threadId, message, {
    session_type: sessionType,
    channel_platform: platform,
    channel_id: channelId,
    channel_metadata: meta,
  });

  if (!ok) {
    return { ok: false, error: "Failed to start run" };
  }

  // 6. Mark as dispatched
  await markDispatched(itemId);

  console.log(`[queue-dispatcher] dispatched queue_id=${itemId} to thread=${threadId} (${source})`);
  return { ok: true, dispatched: true, thread_id: threadId, queue_id: itemId };
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

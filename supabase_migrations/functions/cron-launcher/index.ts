/**
 * cron-launcher — Supabase Edge Function
 *
 * Called by pg_cron for cron jobs (including heartbeat). Posts directly to the
 * agent's most recent idle main session thread. If the thread is busy, queues
 * the message for later delivery by queue-dispatcher.
 *
 * For heartbeat/wake jobs, checks active hours before posting.
 * One-shot jobs are deactivated after posting/queuing.
 *
 * Expected POST body:
 *   { job_name: string, input_message: string, session_type?: string, once?: boolean,
 *     job_id?: number, schedule_type?: string, timezone?: string,
 *     active_hours_start?: string, active_hours_end?: string }
 *
 * session_type defaults to "cron". Heartbeat is identified by job_name="heartbeat".
 * once=true for one-shot jobs — auto-deactivates the pg_cron job after posting.
 *
 * Required env vars (set in Supabase dashboard):
 *   LANGGRAPH_API_URL — base URL of the LangGraph server
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const LANGGRAPH_API_URL = Deno.env.get("LANGGRAPH_API_URL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

/**
 * Check if current time is within active hours for the given timezone.
 * Supports midnight wrap-around (e.g. 22:00–06:00).
 * Returns true if no active hours are configured (no filtering).
 */
function isWithinActiveHours(
  timezone?: string,
  activeHoursStart?: string,
  activeHoursEnd?: string,
): boolean {
  if (!timezone || !activeHoursStart || !activeHoursEnd) {
    return true;
  }

  const toMinutes = (hhmm: string): number => {
    const [h, m] = hhmm.split(":").map(Number);
    return h * 60 + m;
  };

  const startMin = toMinutes(activeHoursStart);
  const endMin = toMinutes(activeHoursEnd);

  if (startMin === endMin) {
    return false;
  }

  const now = new Date();
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: timezone,
    hour: "numeric",
    minute: "numeric",
    hour12: false,
  });
  const parts = formatter.formatToParts(now);
  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? 0);
  const minute = Number(parts.find((p) => p.type === "minute")?.value ?? 0);
  const currentMin = hour * 60 + minute;

  if (endMin > startMin) {
    return currentMin >= startMin && currentMin < endMin;
  }

  return currentMin >= startMin || currentMin < endMin;
}

function buildInjectedMessage(
  jobName: string,
  inputMessage: string,
  jobId?: number,
  scheduleType?: string,
): string {
  const isHeartbeat = jobName === "heartbeat" || jobName === "wake";

  const attrs = [`type="${isHeartbeat ? "heartbeat" : "cron-job"}"`];
  attrs.push(`job_name="${jobName}"`);
  if (jobId != null) attrs.push(`job_id="${jobId}"`);
  if (scheduleType) attrs.push(`schedule_type="${scheduleType}"`);

  const content = isHeartbeat ? "[HEARTBEAT]" : inputMessage;

  return `<system-message ${attrs.join(" ")}>\n${content}\n</system-message>`;
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

// ---------------------------------------------------------------------------
// LangGraph helpers
// ---------------------------------------------------------------------------

const lgHeaders = { "Content-Type": "application/json" };

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
    console.error(`[cron-launcher] thread search failed: ${resp.status}`);
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
    console.error(`[cron-launcher] create thread failed: ${resp.status}`);
    return null;
  }
  const data = await resp.json();
  return data.thread_id;
}

async function startRun(
  threadId: string,
  message: string,
  input: Record<string, unknown>,
): Promise<{ ok: boolean; status?: number }> {
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
    console.error(`[cron-launcher] start run failed ${resp.status}: ${text}`);
    return { ok: false, status: resp.status };
  }
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Queue fallback
// ---------------------------------------------------------------------------

async function queueForThread(
  threadId: string,
  message: string,
  source: string,
  priority: number,
  metadata: Record<string, unknown>,
): Promise<boolean> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_queue`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify({
      source,
      priority,
      thread_id: threadId,
      buffer_key: `${source}:${threadId}`,
      combined_text: message,
      metadata,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[cron-launcher] queue insert failed ${resp.status}: ${text}`);
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// Main logic
// ---------------------------------------------------------------------------

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  let body: {
    job_name?: string; input_message?: string; session_type?: string;
    once?: boolean; job_id?: number; schedule_type?: string;
    timezone?: string; active_hours_start?: string; active_hours_end?: string;
  };
  try {
    body = await req.json();
  } catch {
    return new Response("Invalid JSON body", { status: 400 });
  }

  const { job_name, input_message } = body;
  const sessionType = body.session_type ?? "cron";
  if (!job_name || !input_message) {
    return new Response("Missing job_name or input_message", { status: 400 });
  }

  // Active hours gate — skip heartbeat/wake if outside configured hours
  const isHeartbeat = job_name === "heartbeat" || job_name === "wake";
  if (isHeartbeat && !isWithinActiveHours(body.timezone, body.active_hours_start, body.active_hours_end)) {
    console.log(`[cron-launcher] job=${job_name} outside active hours, skipping`);
    return new Response(JSON.stringify({ ok: true, skipped: "outside_active_hours" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Build the message with system-message tag
  const message = buildInjectedMessage(job_name, input_message, body.job_id, body.schedule_type);
  const priority = isHeartbeat ? 5 : 4;
  const source = isHeartbeat ? "heartbeat" : "cron";
  const metadata = {
    job_name,
    job_id: body.job_id,
    session_type: sessionType,
    schedule_type: body.schedule_type,
    input_message,
  };

  // Find main thread and try to post directly
  const mainThread = await findMainThread();
  let threadId: string;
  let dispatched = false;

  if (mainThread && mainThread.status !== "busy") {
    threadId = mainThread.thread_id as string;
    const runInput: Record<string, unknown> = { session_type: "main" };
    if (body.job_id != null) runInput.cron_job_id = body.job_id;
    if (job_name) runInput.cron_job_name = job_name;
    if (body.schedule_type) runInput.cron_schedule_type = body.schedule_type;

    const result = await startRun(threadId, message, runInput);
    if (result.ok) {
      dispatched = true;
    } else if (result.status === 409) {
      // Thread became busy between check and post — queue it
      await queueForThread(threadId, message, source, priority, metadata);
    } else {
      return new Response(JSON.stringify({ ok: false, error: "Failed to start run" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
  } else if (mainThread && mainThread.status === "busy") {
    // Main thread is busy — queue for it
    threadId = mainThread.thread_id as string;
    await queueForThread(threadId, message, source, priority, metadata);
  } else {
    // No main thread — create one and post
    const newId = await createThread();
    if (!newId) {
      return new Response(JSON.stringify({ ok: false, error: "Failed to create thread" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
    threadId = newId;
    const runInput: Record<string, unknown> = { session_type: "main" };
    if (body.job_id != null) runInput.cron_job_id = body.job_id;
    if (job_name) runInput.cron_job_name = job_name;
    if (body.schedule_type) runInput.cron_schedule_type = body.schedule_type;

    const result = await startRun(threadId, message, runInput);
    if (!result.ok) {
      return new Response(JSON.stringify({ ok: false, error: "Failed to start run on new thread" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
    dispatched = true;
  }

  // Deactivate one-shot jobs
  if (body.once && body.job_id != null && SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY) {
    try {
      const deactivateResp = await fetch(
        `${SUPABASE_URL}/rest/v1/rpc/update_agent_cron`,
        {
          method: "POST",
          headers: supabaseHeaders(),
          body: JSON.stringify({ job_id: body.job_id, new_active: false }),
        },
      );
      if (!deactivateResp.ok) {
        const text = await deactivateResp.text();
        console.error(`[cron-launcher] deactivate failed for ${job_name}: ${deactivateResp.status} ${text}`);
      } else {
        console.log(`[cron-launcher] one-shot job ${job_name} deactivated`);
      }
    } catch (err) {
      console.error(`[cron-launcher] deactivate error for ${job_name}:`, err);
    }
  }

  console.log(`[cron-launcher] job=${job_name} ${dispatched ? "dispatched" : "queued"} thread=${threadId}`);
  return new Response(JSON.stringify({ ok: true, dispatched, thread_id: threadId }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

/**
 * cron-launcher — Supabase Edge Function
 *
 * Called by pg_cron for cron jobs (including heartbeat). Instead of creating
 * LangGraph threads directly, inserts into `inbound_queue` so the
 * queue-dispatcher handles dispatch to the agent's main session thread.
 *
 * For heartbeat/wake jobs, checks active hours before queuing.
 * One-shot jobs are deactivated after queuing.
 *
 * Expected POST body:
 *   { job_name: string, input_message: string, session_type?: string, once?: boolean,
 *     job_id?: number, schedule_type?: string, timezone?: string,
 *     active_hours_start?: string, active_hours_end?: string }
 *
 * session_type defaults to "cron". Heartbeat is identified by job_name="heartbeat".
 * once=true for one-shot jobs — auto-deactivates the pg_cron job after queuing.
 *
 * Required env vars (set in Supabase dashboard):
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

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

  // Determine priority: heartbeat=5, cron=4
  const priority = isHeartbeat ? 5 : 4;

  // Insert into inbound_queue (dispatcher will pick it up)
  const queueRow = {
    source: isHeartbeat ? "heartbeat" : "cron",
    priority,
    buffer_key: `cron:${job_name}`,
    combined_text: message,
    metadata: {
      job_name,
      job_id: body.job_id,
      session_type: sessionType,
      schedule_type: body.schedule_type,
      input_message,
    },
  };

  try {
    const resp = await fetch(`${SUPABASE_URL}/rest/v1/inbound_queue`, {
      method: "POST",
      headers: supabaseHeaders(),
      body: JSON.stringify(queueRow),
    });
    if (!resp.ok) {
      const text = await resp.text();
      console.error(`[cron-launcher] queue insert failed ${resp.status}: ${text}`);
      return new Response(`Failed to queue: ${resp.status}`, { status: 502 });
    }
  } catch (err) {
    console.error("[cron-launcher] queue insert error:", err);
    return new Response(`Queue insert error: ${err}`, { status: 502 });
  }

  // Deactivate one-shot jobs (keep for history, prevent re-firing)
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

  console.log(`[cron-launcher] job=${job_name} queued (priority=${priority})`);
  return new Response(JSON.stringify({ ok: true, queued: true }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

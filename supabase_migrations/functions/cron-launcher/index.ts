/**
 * cron-launcher — Supabase Edge Function
 *
 * Called by pg_cron for cron jobs (including heartbeat). Bridges the two-call
 * requirement (create thread + start run) that pg_cron cannot do in a single
 * net.http_post. Constructs the injected message with a tag and delivery
 * instructions before starting the run.
 *
 * Expected POST body:
 *   { job_name: string, input_message: string, session_type?: string, once?: boolean, job_id?: number, schedule_type?: string }
 *
 * session_type defaults to "cron". Heartbeat is identified by job_name="heartbeat".
 * once=true for one-shot jobs — auto-deactivates the pg_cron job after firing.
 *
 * Required env vars (set in Supabase dashboard):
 *   LANGGRAPH_API_URL — base URL of the LangGraph server
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const LANGGRAPH_API_URL = Deno.env.get("LANGGRAPH_API_URL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

function buildInjectedMessage(
  jobName: string,
  inputMessage: string,
  jobId?: number,
  scheduleType?: string,
): string {
  const isHeartbeat = jobName === "heartbeat" || jobName === "wake";

  // Build attributes for system-message tag
  const attrs = [`type="${isHeartbeat ? "heartbeat" : "cron-job"}"`];
  attrs.push(`job_name="${jobName}"`);
  if (jobId != null) attrs.push(`job_id="${jobId}"`);
  if (scheduleType) attrs.push(`schedule_type="${scheduleType}"`);

  const content = isHeartbeat ? "[HEARTBEAT]" : inputMessage;

  const delivery =
    `Focus only on the task above. When done, you MUST report back to the latest main session ` +
    `— this is not optional, even if there's nothing actionable. Report: what you did (or checked), ` +
    `any outputs or content produced (include links where appropriate), and anything that needs ` +
    `follow-up. Give enough context that the main session can act on it — whether that's updating ` +
    `the user, doing follow-on work, or just filing it away. ` +
    `If rejected (e.g. session is busy), send to a new main session instead.`;

  return `<system-message ${attrs.join(" ")}>\n${content}\n\n${delivery}\n</system-message>`;
}

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  let body: { job_name?: string; input_message?: string; session_type?: string; once?: boolean; job_id?: number; schedule_type?: string };
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

  if (!LANGGRAPH_API_URL) {
    return new Response("LANGGRAPH_API_URL not configured", { status: 500 });
  }

  const headers = { "Content-Type": "application/json" };

  // Step 1: create a fresh thread for this firing
  let threadId: string;
  try {
    const r1 = await fetch(`${LANGGRAPH_API_URL}/threads`, {
      method: "POST",
      headers,
      body: JSON.stringify({}),
    });
    if (!r1.ok) {
      const text = await r1.text();
      console.error(`[cron-launcher] create thread failed ${r1.status}: ${text}`);
      return new Response(`Failed to create thread: ${r1.status}`, { status: 502 });
    }
    const t = await r1.json();
    threadId = t.thread_id;
  } catch (err) {
    console.error("[cron-launcher] create thread error:", err);
    return new Response(`Create thread error: ${err}`, { status: 502 });
  }

  // Step 2: start a run with the given session_type and injected message
  const message = buildInjectedMessage(job_name, input_message, body.job_id, body.schedule_type);
  const runInput: Record<string, unknown> = {
    messages: [{ role: "user", content: message }],
    session_type: sessionType,
  };
  if (body.job_id != null) {
    runInput.cron_job_id = body.job_id;
  }
  if (body.job_name) {
    runInput.cron_job_name = body.job_name;
  }
  if (body.schedule_type) {
    runInput.cron_schedule_type = body.schedule_type;
  }
  try {
    const r2 = await fetch(`${LANGGRAPH_API_URL}/threads/${threadId}/runs`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        assistant_id: "main",
        input: runInput,
        stream_resumable: true,
      }),
    });
    if (!r2.ok) {
      const text = await r2.text();
      console.error(`[cron-launcher] start run failed ${r2.status}: ${text}`);
      return new Response(`Failed to start run: ${r2.status}`, { status: 502 });
    }
  } catch (err) {
    console.error("[cron-launcher] start run error:", err);
    return new Response(`Start run error: ${err}`, { status: 502 });
  }

  // Step 3: deactivate one-shot jobs (keep for history, prevent re-firing)
  if (body.once && body.job_id != null && SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY) {
    try {
      const deactivateResp = await fetch(
        `${SUPABASE_URL}/rest/v1/rpc/update_agent_cron`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
          },
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

  console.log(`[cron-launcher] job=${job_name} thread=${threadId} started`);
  return new Response(JSON.stringify({ ok: true, thread_id: threadId }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

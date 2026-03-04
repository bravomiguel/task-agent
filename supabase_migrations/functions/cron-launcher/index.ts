/**
 * cron-launcher — Supabase Edge Function
 *
 * Called by pg_cron for cron and heartbeat jobs. Bridges the two-call
 * requirement (create thread + start run) that pg_cron cannot do in a single
 * net.http_post. Constructs the injected message with a tag and delivery
 * instructions before starting the run.
 *
 * Expected POST body:
 *   { job_name: string, input_message: string, session_type?: string, once?: boolean }
 *
 * session_type defaults to "cron". Use "heartbeat" for heartbeat jobs.
 * once=true for one-shot jobs — auto-deletes the pg_cron job after firing.
 *
 * Required env vars (set in Supabase dashboard):
 *   LANGGRAPH_API_URL — base URL of the LangGraph server
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const LANGGRAPH_API_URL = Deno.env.get("LANGGRAPH_API_URL") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

function buildTag(sessionType: string, jobName: string): string {
  return sessionType === "heartbeat" ? "[HEARTBEAT]" : `[CRON:${jobName}]`;
}

function buildInjectedMessage(
  sessionType: string,
  jobName: string,
  inputMessage: string,
): string {
  const tag = buildTag(sessionType, jobName);
  return (
    `${tag}\n` +
    `${inputMessage}\n\n` +
    `---\n` +
    `When your work is done: use sessions_list to find the latest main session ` +
    `(look for session_type="main" in thread values), ` +
    `then sessions_send to deliver your summary. ` +
    `Start your summary message with ${tag} so it can be identified. ` +
    `If the latest main session is busy, or if sessions_send fails for any other reason, ` +
    `use sessions_spawn with session_type="main" instead. ` +
    `If there is nothing to report, respond with NO_REPLY.`
  );
}

serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  let body: { job_name?: string; input_message?: string; session_type?: string; once?: boolean };
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
  const message = buildInjectedMessage(sessionType, job_name, input_message);
  try {
    const r2 = await fetch(`${LANGGRAPH_API_URL}/threads/${threadId}/runs`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        assistant_id: "agent",
        input: {
          messages: [{ role: "user", content: message }],
          session_type: sessionType,
        },
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

  // Step 3: self-cleanup for one-shot jobs
  if (body.once && SUPABASE_URL && SUPABASE_SERVICE_ROLE_KEY) {
    try {
      const cleanupResp = await fetch(
        `${SUPABASE_URL}/rest/v1/rpc/delete_agent_cron`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
          },
          body: JSON.stringify({ job_name }),
        },
      );
      if (!cleanupResp.ok) {
        const text = await cleanupResp.text();
        console.error(`[cron-launcher] cleanup failed for ${job_name}: ${cleanupResp.status} ${text}`);
      } else {
        console.log(`[cron-launcher] one-shot job ${job_name} cleaned up`);
      }
    } catch (err) {
      console.error(`[cron-launcher] cleanup error for ${job_name}:`, err);
    }
  }

  console.log(`[cron-launcher] job=${job_name} thread=${threadId} started`);
  return new Response(JSON.stringify({ ok: true, thread_id: threadId }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
});

/**
 * teams-subscriptions — Supabase Edge Function
 *
 * Manages Microsoft Graph subscriptions for Teams chat and channel messages.
 * Subscriptions expire after ~60 minutes, so this function handles creation,
 * renewal, and listing.
 *
 * Routes:
 *   POST /teams-subscriptions/subscribe — Create subscriptions for all chats + channels
 *   POST /teams-subscriptions/renew     — Renew all active subscriptions before expiry
 *   POST /teams-subscriptions/list      — List current subscriptions
 *
 * Required env vars:
 *   COMPOSIO_API_KEY — for fetching Microsoft access token via Composio
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 *
 * Vault secrets:
 *   teams_composio_connection_id — Composio connected account ID for Microsoft
 *   teams_webhook_secret — clientState for subscription validation
 *   teams_webhook_url — Notification URL (e.g. https://<project>.supabase.co/functions/v1/channel-webhook/teams)
 */

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";

const COMPOSIO_API_KEY = Deno.env.get("COMPOSIO_API_KEY") ?? "";
const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

const GRAPH_API = "https://graph.microsoft.com/v1.0";

// Max subscription lifetime for chat/channel message resources
const SUBSCRIPTION_LIFETIME_MINUTES = 59;

// ---------------------------------------------------------------------------
// Supabase helpers
// ---------------------------------------------------------------------------

function supabaseHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${SUPABASE_SERVICE_ROLE_KEY}`,
    apikey: SUPABASE_SERVICE_ROLE_KEY,
  };
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

async function upsertSubscription(row: Record<string, unknown>): Promise<void> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/teams_subscriptions`, {
    method: "POST",
    headers: {
      ...supabaseHeaders(),
      Prefer: "return=minimal,resolution=merge-duplicates",
    },
    body: JSON.stringify(row),
  });
  if (!resp.ok) {
    const text = await resp.text();
    console.error(`[teams-subscriptions] upsert failed ${resp.status}: ${text}`);
  }
}

async function deleteSubscriptionRow(subscriptionId: string): Promise<void> {
  const resp = await fetch(
    `${SUPABASE_URL}/rest/v1/teams_subscriptions?subscription_id=eq.${encodeURIComponent(subscriptionId)}`,
    {
      method: "DELETE",
      headers: supabaseHeaders(),
    },
  );
  if (!resp.ok) {
    console.error(`[teams-subscriptions] delete row failed: ${resp.status}`);
  }
}

async function getActiveSubscriptions(): Promise<Record<string, unknown>[]> {
  const resp = await fetch(
    `${SUPABASE_URL}/rest/v1/teams_subscriptions?order=expires_at.asc`,
    { headers: supabaseHeaders() },
  );
  if (!resp.ok) return [];
  return await resp.json();
}

// ---------------------------------------------------------------------------
// Composio token
// ---------------------------------------------------------------------------

async function fetchAccessToken(connectionId: string): Promise<string | null> {
  if (!COMPOSIO_API_KEY) return null;
  try {
    const resp = await fetch(
      `https://backend.composio.dev/api/v3/connected_accounts/${connectionId}`,
      { headers: { "x-api-key": COMPOSIO_API_KEY } },
    );
    if (!resp.ok) return null;
    const data = await resp.json();
    return data?.data?.access_token ?? null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Microsoft Graph helpers
// ---------------------------------------------------------------------------

interface TeamsChat {
  id: string;
  topic: string | null;
  chatType: string;
}

interface TeamsTeam {
  id: string;
  displayName: string;
}

interface TeamsChannel {
  id: string;
  displayName: string;
}

async function graphGet<T>(path: string, token: string): Promise<T[]> {
  const resp = await fetch(`${GRAPH_API}${path}`, {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Graph ${path} failed: ${resp.status} - ${text}`);
  }
  const data = await resp.json();
  return (data.value ?? []) as T[];
}

async function listChats(token: string): Promise<TeamsChat[]> {
  return graphGet<TeamsChat>("/me/chats", token);
}

async function listTeams(token: string): Promise<TeamsTeam[]> {
  return graphGet<TeamsTeam>("/me/joinedTeams", token);
}

async function listChannels(teamId: string, token: string): Promise<TeamsChannel[]> {
  return graphGet<TeamsChannel>(`/teams/${encodeURIComponent(teamId)}/channels`, token);
}

async function createGraphSubscription(
  resource: string,
  notificationUrl: string,
  clientState: string,
  expirationDateTime: Date,
  token: string,
): Promise<Record<string, unknown>> {
  const resp = await fetch(`${GRAPH_API}/subscriptions`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      changeType: "created",
      notificationUrl,
      resource,
      expirationDateTime: expirationDateTime.toISOString(),
      clientState,
    }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} - ${text}`);
  }
  return await resp.json();
}

async function renewGraphSubscription(
  subscriptionId: string,
  expirationDateTime: Date,
  token: string,
): Promise<Record<string, unknown> | null> {
  const resp = await fetch(`${GRAPH_API}/subscriptions/${subscriptionId}`, {
    method: "PATCH",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      expirationDateTime: expirationDateTime.toISOString(),
    }),
  });
  if (!resp.ok) {
    console.error(`[teams-subscriptions] renew ${subscriptionId} failed: ${resp.status}`);
    return null;
  }
  return await resp.json();
}

async function deleteGraphSubscription(subscriptionId: string, token: string): Promise<boolean> {
  const resp = await fetch(`${GRAPH_API}/subscriptions/${subscriptionId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  return resp.ok || resp.status === 404;
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

function jsonResponse(data: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function handleSubscribe(token: string, notificationUrl: string, clientState: string): Promise<Response> {
  const expiration = new Date();
  expiration.setMinutes(expiration.getMinutes() + SUBSCRIPTION_LIFETIME_MINUTES);

  const successful: Array<Record<string, unknown>> = [];
  const failed: Array<Record<string, unknown>> = [];

  // Subscribe to all chats
  try {
    const chats = await listChats(token);
    console.log(`[teams-subscriptions] found ${chats.length} chats`);

    for (const chat of chats) {
      const resource = `/chats/${chat.id}/messages`;
      const name = chat.topic || `${chat.chatType} chat`;
      try {
        const sub = await createGraphSubscription(resource, notificationUrl, clientState, expiration, token);
        await upsertSubscription({
          subscription_id: sub.id,
          resource,
          resource_type: "chat",
          resource_name: name,
          expires_at: sub.expirationDateTime,
        });
        successful.push({ id: sub.id, resource, type: "chat", name });
        console.log(`[teams-subscriptions] subscribed to chat: ${name}`);
      } catch (e) {
        const error = e instanceof Error ? e.message : String(e);
        failed.push({ resource, type: "chat", name, error });
        console.error(`[teams-subscriptions] failed chat ${name}: ${error}`);
      }
    }
  } catch (e) {
    console.error("[teams-subscriptions] failed to list chats:", e);
  }

  // Subscribe to all team channels
  try {
    const teams = await listTeams(token);
    console.log(`[teams-subscriptions] found ${teams.length} teams`);

    for (const team of teams) {
      try {
        const channels = await listChannels(team.id, token);
        for (const channel of channels) {
          const resource = `/teams/${team.id}/channels/${channel.id}/messages`;
          const name = `${team.displayName} > ${channel.displayName}`;
          try {
            const sub = await createGraphSubscription(resource, notificationUrl, clientState, expiration, token);
            await upsertSubscription({
              subscription_id: sub.id,
              resource,
              resource_type: "channel",
              resource_name: name,
              expires_at: sub.expirationDateTime,
            });
            successful.push({ id: sub.id, resource, type: "channel", name });
            console.log(`[teams-subscriptions] subscribed to channel: ${name}`);
          } catch (e) {
            const error = e instanceof Error ? e.message : String(e);
            failed.push({ resource, type: "channel", name, error });
            console.error(`[teams-subscriptions] failed channel ${name}: ${error}`);
          }
        }
      } catch (e) {
        console.error(`[teams-subscriptions] failed to list channels for ${team.displayName}:`, e);
      }
    }
  } catch (e) {
    console.error("[teams-subscriptions] failed to list teams:", e);
  }

  return jsonResponse({
    ok: true,
    action: "subscribe",
    successful: successful.length,
    failed: failed.length,
    details: { successful, failed },
  });
}

async function handleRenew(token: string): Promise<Response> {
  const subs = await getActiveSubscriptions();
  if (subs.length === 0) {
    return jsonResponse({ ok: true, action: "renew", message: "no_subscriptions" });
  }

  const expiration = new Date();
  expiration.setMinutes(expiration.getMinutes() + SUBSCRIPTION_LIFETIME_MINUTES);

  let renewed = 0;
  let failed = 0;
  let deleted = 0;

  for (const sub of subs) {
    const subId = sub.subscription_id as string;
    const result = await renewGraphSubscription(subId, expiration, token);
    if (result) {
      // Update expiration in DB
      await upsertSubscription({
        subscription_id: subId,
        resource: sub.resource,
        resource_type: sub.resource_type,
        resource_name: sub.resource_name,
        expires_at: result.expirationDateTime,
      });
      renewed++;
    } else {
      // Subscription expired or invalid — clean up row
      await deleteSubscriptionRow(subId);
      deleted++;
      failed++;
    }
  }

  console.log(`[teams-subscriptions] renewed=${renewed} failed=${failed} deleted=${deleted}`);
  return jsonResponse({ ok: true, action: "renew", renewed, failed, deleted });
}

async function handleList(): Promise<Response> {
  const subs = await getActiveSubscriptions();
  return jsonResponse({
    ok: true,
    action: "list",
    count: subs.length,
    subscriptions: subs.map((s) => ({
      id: s.subscription_id,
      resource: s.resource,
      type: s.resource_type,
      name: s.resource_name,
      expires_at: s.expires_at,
    })),
  });
}

async function handleUnsubscribe(token: string): Promise<Response> {
  const subs = await getActiveSubscriptions();
  let deleted = 0;
  let failed = 0;

  for (const sub of subs) {
    const subId = sub.subscription_id as string;
    const ok = await deleteGraphSubscription(subId, token);
    if (ok) {
      await deleteSubscriptionRow(subId);
      deleted++;
    } else {
      failed++;
    }
  }

  return jsonResponse({ ok: true, action: "unsubscribe", deleted, failed });
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

  // Determine action from path
  let action: string;
  if (path.endsWith("/subscribe")) action = "subscribe";
  else if (path.endsWith("/renew")) action = "renew";
  else if (path.endsWith("/list")) action = "list";
  else if (path.endsWith("/unsubscribe")) action = "unsubscribe";
  else {
    // Try from body
    try {
      const body = await req.json();
      action = (body.action as string) ?? "";
    } catch {
      action = "";
    }
  }

  if (!action || !["subscribe", "renew", "list", "unsubscribe"].includes(action)) {
    return jsonResponse({ error: "Unknown action. Use subscribe, renew, list, or unsubscribe." }, 400);
  }

  // List doesn't need a token
  if (action === "list") {
    return handleList();
  }

  // Get Composio connection ID from vault
  const connectionId = await getVaultSecret("teams_composio_connection_id");
  if (!connectionId) {
    return jsonResponse({ ok: false, error: "teams_composio_connection_id not in vault" }, 500);
  }

  const token = await fetchAccessToken(connectionId);
  if (!token) {
    return jsonResponse({ ok: false, error: "Failed to fetch Microsoft access token" }, 500);
  }

  if (action === "subscribe") {
    const webhookUrl = await getVaultSecret("teams_webhook_url");
    const clientState = await getVaultSecret("teams_webhook_secret");
    if (!webhookUrl || !clientState) {
      return jsonResponse({
        ok: false,
        error: "Missing vault secrets: teams_webhook_url and/or teams_webhook_secret",
      }, 500);
    }
    return handleSubscribe(token, webhookUrl, clientState);
  }

  if (action === "renew") {
    return handleRenew(token);
  }

  if (action === "unsubscribe") {
    return handleUnsubscribe(token);
  }

  return jsonResponse({ error: "Unreachable" }, 500);
});

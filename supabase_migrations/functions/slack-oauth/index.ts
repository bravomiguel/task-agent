/**
 * slack-oauth — Supabase Edge Function
 *
 * Handles Slack OAuth 2.0 "Add to Slack" flow for chat surface setup.
 * No manual token copying — user clicks a link, authorizes, tokens stored automatically.
 *
 * Routes:
 *   GET /slack-oauth/install   — Redirects user to Slack OAuth authorize page
 *   GET /slack-oauth/callback  — Slack redirects here after authorization
 *
 * Required env vars:
 *   SLACK_CLIENT_ID       — Slack app client ID
 *   SLACK_CLIENT_SECRET   — Slack app client secret
 *   SLACK_SIGNING_SECRET  — Slack app signing secret (stored in vault for webhook verification)
 *   SUPABASE_URL          — Supabase project URL
 *   SUPABASE_SERVICE_ROLE_KEY — For vault access
 */

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const SLACK_CLIENT_ID = Deno.env.get("SLACK_CLIENT_ID") ?? "";
const SLACK_CLIENT_SECRET = Deno.env.get("SLACK_CLIENT_SECRET") ?? "";
const SLACK_SIGNING_SECRET = Deno.env.get("SLACK_SIGNING_SECRET") ?? "";

// Bot scopes matching the existing manifest
const BOT_SCOPES = [
  "chat:write",
  "channels:read",
  "channels:history",
  "groups:read",
  "groups:history",
  "im:read",
  "im:history",
  "mpim:read",
  "mpim:history",
  "app_mentions:read",
  "users:read",
  "reactions:write",
].join(",");

function supabaseHeaders(): Record<string, string> {
  return {
    apikey: SUPABASE_SERVICE_KEY,
    Authorization: `Bearer ${SUPABASE_SERVICE_KEY}`,
    "Content-Type": "application/json",
  };
}

async function setVaultSecret(name: string, value: string): Promise<void> {
  const resp = await fetch(`${SUPABASE_URL}/rest/v1/rpc/set_vault_secret`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify({ p_name: name, p_secret: value }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Failed to set vault secret ${name}: ${resp.status} ${text}`);
  }
}

function htmlResponse(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function redirectResponse(url: string): Response {
  return new Response(null, {
    status: 302,
    headers: { Location: url },
  });
}

// ---------------------------------------------------------------------------
// /install — redirect to Slack OAuth authorize page
// ---------------------------------------------------------------------------

function handleInstall(requestUrl: URL): Response {
  if (!SLACK_CLIENT_ID) {
    return htmlResponse("<h1>Error</h1><p>SLACK_CLIENT_ID not configured.</p>", 500);
  }

  const callbackUrl = `${SUPABASE_URL}/functions/v1/slack-oauth/callback`;

  const authorizeUrl = new URL("https://slack.com/oauth/v2/authorize");
  authorizeUrl.searchParams.set("client_id", SLACK_CLIENT_ID);
  authorizeUrl.searchParams.set("scope", BOT_SCOPES);
  authorizeUrl.searchParams.set("redirect_uri", callbackUrl);

  return redirectResponse(authorizeUrl.toString());
}

// ---------------------------------------------------------------------------
// /callback — exchange code for token, store in vault
// ---------------------------------------------------------------------------

async function handleCallback(requestUrl: URL): Promise<Response> {
  const code = requestUrl.searchParams.get("code");
  const error = requestUrl.searchParams.get("error");

  if (error) {
    console.error(`[slack-oauth] authorization denied: ${error}`);
    return htmlResponse(
      `<h1>Setup cancelled</h1><p>Slack authorization was denied: ${error}</p>
       <p>You can close this tab and try again.</p>`,
    );
  }

  if (!code) {
    return htmlResponse("<h1>Error</h1><p>No authorization code received.</p>", 400);
  }

  // Exchange code for token
  const callbackUrl = `${SUPABASE_URL}/functions/v1/slack-oauth/callback`;
  const tokenResp = await fetch("https://slack.com/api/oauth.v2.access", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: SLACK_CLIENT_ID,
      client_secret: SLACK_CLIENT_SECRET,
      code,
      redirect_uri: callbackUrl,
    }),
  });

  const tokenData = await tokenResp.json();

  if (!tokenData.ok) {
    console.error(`[slack-oauth] token exchange failed: ${tokenData.error}`);
    return htmlResponse(
      `<h1>Setup failed</h1><p>Token exchange failed: ${tokenData.error}</p>
       <p>You can close this tab and try again.</p>`,
      500,
    );
  }

  const botToken = tokenData.access_token;
  const botUserId = tokenData.bot_user_id;
  const teamName = tokenData.team?.name ?? "unknown";
  const installingUserId = tokenData.authed_user?.id;

  if (!botToken) {
    return htmlResponse("<h1>Error</h1><p>No bot token in response.</p>", 500);
  }

  console.log(
    `[slack-oauth] installed to workspace: ${teamName}, bot: ${botUserId}, installer: ${installingUserId}`,
  );

  // Store everything in vault
  try {
    await setVaultSecret("slack_bot_token", botToken);
    await setVaultSecret("slack_bot_user_id", botUserId);
    await setVaultSecret("slack_signing_secret", SLACK_SIGNING_SECRET);
    if (installingUserId) {
      await setVaultSecret("slack_bot_owner_id", installingUserId);
    }
    console.log("[slack-oauth] all secrets stored in vault");
  } catch (e) {
    console.error(`[slack-oauth] failed to store secrets: ${e}`);
    return htmlResponse(
      `<h1>Setup failed</h1><p>Token obtained but failed to store credentials.</p>
       <p>Please try again or contact support.</p>`,
      500,
    );
  }

  // Success page
  return htmlResponse(`
    <!DOCTYPE html>
    <html>
    <head><title>Slack Connected</title></head>
    <body style="font-family: -apple-system, sans-serif; max-width: 500px; margin: 80px auto; text-align: center;">
      <h1>Slack connected!</h1>
      <p>You can now chat with your assistant on Slack.</p>
      <p>Workspace: <strong>${teamName}</strong></p>
      <p style="color: #666; margin-top: 24px;">You can close this tab.</p>
    </body>
    </html>
  `);
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  const url = new URL(req.url);
  const path = url.pathname.replace(/^\/slack-oauth\/?/, "").replace(/\/$/, "");

  try {
    if (path === "install" && req.method === "GET") {
      return handleInstall(url);
    }
    if (path === "callback" && req.method === "GET") {
      return await handleCallback(url);
    }
    return new Response("Not found", { status: 404 });
  } catch (e) {
    console.error(`[slack-oauth] unhandled error: ${e}`);
    return htmlResponse("<h1>Error</h1><p>An unexpected error occurred.</p>", 500);
  }
});

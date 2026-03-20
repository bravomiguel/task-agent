/**
 * teams-bot-oauth — Supabase Edge Function
 *
 * Handles Teams bot setup via admin consent OAuth flow.
 * User clicks an "Add to Teams" link, grants admin consent, and the bot
 * is installed in their tenant. Credentials are stored in vault.
 *
 * Routes:
 *   GET /teams-bot-oauth/install   — Redirects to Microsoft admin consent page
 *   GET /teams-bot-oauth/callback  — Microsoft redirects here after consent
 *
 * The bot receives messages via the Bot Framework messaging endpoint at
 * channel-webhook/teams-bot. On first message, a Graph subscription is
 * created for that specific chat (deferred subscription pattern).
 *
 * Required env vars:
 *   TEAMS_BOT_APP_ID       — Azure AD app (client) ID for the bot
 *   TEAMS_BOT_APP_SECRET   — Azure AD app client secret
 *   TEAMS_BOT_TENANT_ID    — Azure AD tenant ID (or "common" for multi-tenant)
 *   SUPABASE_URL           — Supabase project URL
 *   SUPABASE_SERVICE_ROLE_KEY — For vault access
 */

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const TEAMS_BOT_APP_ID = Deno.env.get("TEAMS_BOT_APP_ID") ?? "";
const TEAMS_BOT_APP_SECRET = Deno.env.get("TEAMS_BOT_APP_SECRET") ?? "";

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
// /install — redirect to Microsoft admin consent page
// ---------------------------------------------------------------------------

function handleInstall(): Response {
  if (!TEAMS_BOT_APP_ID) {
    return htmlResponse("<h1>Error</h1><p>TEAMS_BOT_APP_ID not configured.</p>", 500);
  }

  const callbackUrl = `${SUPABASE_URL}/functions/v1/teams-bot-oauth/callback`;

  // Use admin consent endpoint for org-wide bot installation
  const authorizeUrl = new URL(
    "https://login.microsoftonline.com/common/adminconsent"
  );
  authorizeUrl.searchParams.set("client_id", TEAMS_BOT_APP_ID);
  authorizeUrl.searchParams.set("redirect_uri", callbackUrl);

  return redirectResponse(authorizeUrl.toString());
}

// ---------------------------------------------------------------------------
// /callback — admin consent granted, get app-level token, store in vault
// ---------------------------------------------------------------------------

async function handleCallback(requestUrl: URL): Promise<Response> {
  const error = requestUrl.searchParams.get("error");
  const errorDescription = requestUrl.searchParams.get("error_description");
  const tenantId = requestUrl.searchParams.get("tenant");

  if (error) {
    console.error(`[teams-bot-oauth] consent denied: ${error} — ${errorDescription}`);
    return htmlResponse(
      `<h1>Setup cancelled</h1>
       <p>Microsoft authorization was denied: ${errorDescription || error}</p>
       <p>You can close this tab and try again.</p>`,
    );
  }

  if (!tenantId) {
    return htmlResponse("<h1>Error</h1><p>No tenant ID in callback.</p>", 400);
  }

  console.log(`[teams-bot-oauth] admin consent granted for tenant: ${tenantId}`);

  // Get an app-level access token using client credentials
  try {
    const tokenResp = await fetch(
      `https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`,
      {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          client_id: TEAMS_BOT_APP_ID,
          client_secret: TEAMS_BOT_APP_SECRET,
          scope: "https://graph.microsoft.com/.default",
          grant_type: "client_credentials",
        }),
      }
    );

    const tokenData = await tokenResp.json();

    if (!tokenData.access_token) {
      console.error(`[teams-bot-oauth] token exchange failed:`, tokenData);
      return htmlResponse(
        `<h1>Setup failed</h1><p>Failed to obtain access token.</p>
         <p>You can close this tab and try again.</p>`,
        500,
      );
    }

    // Store credentials in vault
    await setVaultSecret("teams_bot_app_id", TEAMS_BOT_APP_ID);
    await setVaultSecret("teams_bot_app_secret", TEAMS_BOT_APP_SECRET);
    await setVaultSecret("teams_bot_tenant_id", tenantId);

    console.log("[teams-bot-oauth] credentials stored in vault");
  } catch (e) {
    console.error(`[teams-bot-oauth] failed: ${e}`);
    return htmlResponse(
      `<h1>Setup failed</h1><p>Failed to complete setup.</p>
       <p>You can close this tab and try again.</p>`,
      500,
    );
  }

  return htmlResponse(`
    <!DOCTYPE html>
    <html>
    <head><title>Teams Connected</title></head>
    <body style="font-family: -apple-system, sans-serif; max-width: 500px; margin: 80px auto; text-align: center;">
      <h1>Teams connected!</h1>
      <p>You can now chat with your assistant on Microsoft Teams.</p>
      <p>Tenant: <strong>${tenantId}</strong></p>
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
  const path = url.pathname.replace(/^\/teams-bot-oauth\/?/, "").replace(/\/$/, "");

  try {
    if (path === "install" && req.method === "GET") {
      return handleInstall();
    }
    if (path === "callback" && req.method === "GET") {
      return await handleCallback(url);
    }
    return new Response("Not found", { status: 404 });
  } catch (e) {
    console.error(`[teams-bot-oauth] unhandled error: ${e}`);
    return htmlResponse("<h1>Error</h1><p>An unexpected error occurred.</p>", 500);
  }
});

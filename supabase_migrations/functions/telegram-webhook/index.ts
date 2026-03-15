/**
 * telegram-webhook — Supabase Edge Function
 *
 * Receives messages from Telegram Bot API and routes them to the agent.
 * On /start command, stores the user's chat ID in vault to enable the chat surface.
 *
 * Routes:
 *   POST /telegram-webhook — Telegram sends updates here (set via setWebhook API)
 *
 * Required env vars:
 *   TELEGRAM_BOT_TOKEN — Bot token from BotFather
 *   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY — auto-injected by Supabase
 */

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const TELEGRAM_BOT_TOKEN = Deno.env.get("TELEGRAM_BOT_TOKEN") ?? "";

const TELEGRAM_API = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;

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

async function sendTelegramMessage(chatId: string | number, text: string): Promise<void> {
  await fetch(`${TELEGRAM_API}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text }),
  });
}

// ---------------------------------------------------------------------------
// Inbound queue (same pattern as channel-webhook)
// ---------------------------------------------------------------------------

async function queueMessage(
  senderId: string,
  senderName: string,
  messageText: string,
  chatId: string,
): Promise<void> {
  const payload = {
    platform: "telegram",
    sender_id: senderId,
    sender_name: senderName,
    channel_id: chatId,
    channel_type: "dm",
    message_text: messageText,
    message_ts: Date.now().toString(),
  };

  const resp = await fetch(`${SUPABASE_URL}/rest/v1/rpc/queue_inbound_message`, {
    method: "POST",
    headers: supabaseHeaders(),
    body: JSON.stringify(payload),
  });

  if (!resp.ok) {
    // Fall back to direct insert if RPC doesn't exist
    console.error(`[telegram-webhook] queue RPC failed: ${resp.status}`);
  }
}

// ---------------------------------------------------------------------------
// Update handler
// ---------------------------------------------------------------------------

async function handleUpdate(update: Record<string, unknown>): Promise<Response> {
  const message = update.message as Record<string, unknown> | undefined;
  if (!message) {
    // Ignore non-message updates (edited, callback queries, etc.)
    return new Response("ok");
  }

  const chat = message.chat as Record<string, unknown>;
  const from = message.from as Record<string, unknown>;
  const text = (message.text as string) ?? "";
  const chatId = String(chat.id);
  const userId = String(from.id);
  const firstName = (from.first_name as string) ?? "";
  const lastName = (from.last_name as string) ?? "";
  const senderName = [firstName, lastName].filter(Boolean).join(" ") || userId;

  // Check if this is the authorized user
  const ownerChatId = await getVaultSecret("telegram_owner_chat_id");

  // Handle /start command — register the chat surface
  if (text.startsWith("/start")) {
    if (!ownerChatId) {
      // First user to /start becomes the owner
      await setVaultSecret("telegram_owner_chat_id", chatId);
      await setVaultSecret("telegram_owner_user_id", userId);
      await setVaultSecret("telegram_owner_name", senderName);
      console.log(`[telegram-webhook] registered owner: ${senderName} (chat: ${chatId})`);
      await sendTelegramMessage(
        chatId,
        "Connected! You can now chat with me here. Go back and let me know you're set up.",
      );
    } else if (ownerChatId === chatId) {
      await sendTelegramMessage(chatId, "You're already connected! Just send me a message.");
    } else {
      await sendTelegramMessage(chatId, "Sorry, this assistant is already linked to another user.");
    }
    return new Response("ok");
  }

  // Only process messages from the owner
  if (ownerChatId && ownerChatId !== chatId) {
    await sendTelegramMessage(chatId, "Sorry, this assistant is only available to its owner.");
    return new Response("ok");
  }

  if (!ownerChatId) {
    await sendTelegramMessage(chatId, "Please send /start first to set up the connection.");
    return new Response("ok");
  }

  // Route message to agent via inbound queue
  if (text) {
    console.log(`[telegram-webhook] message from ${senderName}: ${text.slice(0, 100)}`);
    await queueMessage(userId, senderName, text, chatId);
  }

  return new Response("ok");
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  try {
    const update = await req.json();
    return await handleUpdate(update);
  } catch (e) {
    console.error(`[telegram-webhook] error: ${e}`);
    return new Response("ok"); // Always return 200 to Telegram to prevent retries
  }
});

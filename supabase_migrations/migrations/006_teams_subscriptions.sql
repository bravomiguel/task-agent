-- Teams subscription tracking + auto-renewal cron.
--
-- Microsoft Graph subscriptions for chat/channel messages expire after ~60 min.
-- This table tracks active subscriptions so they can be renewed automatically.

-- ---------------------------------------------------------------------------
-- Table: teams_subscriptions
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teams_subscriptions (
  subscription_id text PRIMARY KEY,       -- Microsoft Graph subscription ID
  resource text NOT NULL,                  -- e.g. "/chats/{id}/messages"
  resource_type text NOT NULL DEFAULT 'chat', -- "chat" or "channel"
  resource_name text,                      -- human-readable name
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Index for renewal queries (soonest-expiring first)
CREATE INDEX IF NOT EXISTS teams_subscriptions_expires
  ON teams_subscriptions (expires_at);

-- ---------------------------------------------------------------------------
-- pg_cron: renew Teams subscriptions every 50 minutes
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  renew_url text;
  service_key text;
BEGIN
  renew_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'teams_subscriptions_renew_url');
  service_key := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_service_key');

  -- Only create the cron job if the vault secret exists
  IF renew_url IS NOT NULL AND service_key IS NOT NULL THEN
    PERFORM cron.schedule(
      'teams-subscription-renew',
      '*/50 * * * *',
      format(
        'SELECT net.http_post(
          url     := %L,
          body    := ''{"action":"renew"}''::jsonb,
          headers := jsonb_build_object(
            ''Content-Type'',  ''application/json'',
            ''Authorization'', ''Bearer '' || %L
          ),
          timeout_milliseconds := 30000
        )',
        renew_url, service_key
      )
    );
  END IF;
END;
$$;

-- Inbound message queue: buffer + dispatch tables.
--
-- Buffer holds raw messages during the debounce window.
-- Queue holds flushed batches ready for sequential dispatch to the agent.
--
-- Flow:
--   channel-webhook → INSERT inbound_buffer → 5s debounce → flush → INSERT inbound_queue
--   queue-dispatcher (pg_cron, every 10s) → pop oldest undispatched → check main thread idle → dispatch

-- ---------------------------------------------------------------------------
-- Table 1: raw messages awaiting debounce
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inbound_buffer (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text NOT NULL DEFAULT 'slack',
  buffer_key text NOT NULL,           -- grouping key, e.g. "slack:C1234ABCD"
  sender text NOT NULL,               -- sender ID
  sender_name text,                   -- display name
  message_text text NOT NULL,
  metadata jsonb DEFAULT '{}'::jsonb, -- message_ts, thread_ts, channel_id, channel_type, etc.
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Dedup: prevent duplicate Slack messages (same channel + same message_ts)
CREATE UNIQUE INDEX IF NOT EXISTS inbound_buffer_dedup
  ON inbound_buffer (buffer_key, (metadata->>'message_ts'));

-- Flush queries: all messages for a buffer_key, ordered
CREATE INDEX IF NOT EXISTS inbound_buffer_key_created
  ON inbound_buffer (buffer_key, created_at);

-- ---------------------------------------------------------------------------
-- Table 2: flushed batches ready for dispatch
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inbound_queue (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source text NOT NULL DEFAULT 'slack',
  priority int NOT NULL DEFAULT 2,      -- 1=DM, 2=channel, 3=subagent, 4=cron, 5=heartbeat
  thread_id text,                       -- target LangGraph thread (null = route to latest main)
  buffer_key text NOT NULL,
  combined_text text NOT NULL,          -- "[jane] hey\n[mike] yeah" or pre-wrapped system-message
  metadata jsonb DEFAULT '{}'::jsonb,   -- channel_id, thread_ts, senders, message_count, etc.
  created_at timestamptz NOT NULL DEFAULT now(),
  dispatched_at timestamptz             -- null = pending, set when dispatched
);

-- Dispatcher: oldest undispatched per thread, highest priority first
CREATE INDEX IF NOT EXISTS inbound_queue_dispatch
  ON inbound_queue (dispatched_at NULLS FIRST, thread_id, priority, created_at);

-- ---------------------------------------------------------------------------
-- RPC: atomic flush — delete all buffer rows for a key and return them
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION flush_inbound_buffer(p_buffer_key text)
RETURNS TABLE(
  id uuid, sender text, sender_name text, message_text text,
  metadata jsonb, created_at timestamptz
) AS $$
  DELETE FROM inbound_buffer
  WHERE buffer_key = p_buffer_key
  RETURNING id, sender, sender_name, message_text, metadata, created_at;
$$ LANGUAGE sql SECURITY DEFINER;

-- ---------------------------------------------------------------------------
-- pg_cron: queue-dispatcher every 10 seconds
-- ---------------------------------------------------------------------------

DO $$
DECLARE
  dispatcher_url text;
  service_key text;
BEGIN
  dispatcher_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'queue_dispatcher_url');
  service_key := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_service_key');

  PERFORM cron.schedule(
    'queue-dispatcher',
    '10 seconds',
    format(
      'SELECT net.http_post(
        url     := %L,
        body    := ''{}''::jsonb,
        headers := jsonb_build_object(
          ''Content-Type'',  ''application/json'',
          ''Authorization'', ''Bearer '' || %L
        ),
        timeout_milliseconds := 15000
      )',
      dispatcher_url, service_key
    )
  );
END;
$$;

-- ---------------------------------------------------------------------------
-- pg_cron: cleanup orphaned buffer rows (safety net)
-- ---------------------------------------------------------------------------

SELECT cron.schedule(
  'inbound-buffer-cleanup',
  '* * * * *',
  $$DELETE FROM inbound_buffer WHERE created_at < now() - interval '5 minutes'$$
);

-- ---------------------------------------------------------------------------
-- pg_cron: cleanup dispatched queue rows older than 1 hour
-- ---------------------------------------------------------------------------

SELECT cron.schedule(
  'inbound-queue-cleanup',
  '*/5 * * * *',
  $$DELETE FROM inbound_queue WHERE dispatched_at IS NOT NULL AND dispatched_at < now() - interval '1 hour'$$
);

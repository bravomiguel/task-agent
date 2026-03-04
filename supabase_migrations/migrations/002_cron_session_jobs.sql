-- Create a cron job that fires via the cron-launcher Edge Function.
-- The Edge Function handles the two-step LangGraph create-thread + start-run
-- that pg_cron cannot do in a single net.http_post call. It injects a tag and
-- delivery instructions into the agent message.
-- session_type defaults to 'cron'; use 'heartbeat' for heartbeat jobs.

-- Drop older signatures to avoid PostgREST PGRST203 ambiguity
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text);

CREATE OR REPLACE FUNCTION create_cron_session_job(
  job_name text,
  schedule_expr text,
  input_message text,
  session_type text DEFAULT 'cron',
  once boolean DEFAULT false
) RETURNS bigint AS $fn$
DECLARE
  job_id bigint;
  launcher_url text;
  service_key text;
  body jsonb;
BEGIN
  launcher_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'cron_launcher_url');
  service_key  := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_service_key');

  body := jsonb_build_object(
    'job_name',      job_name,
    'input_message', input_message,
    'session_type',  session_type,
    'once',          once
  );

  SELECT cron.schedule(
    job_name,
    schedule_expr,
    format(
      $$SELECT net.http_post(
          url     := %L,
          body    := %L::jsonb,
          headers := jsonb_build_object(
            'Content-Type',  'application/json',
            'Authorization', 'Bearer ' || %L
          ),
          timeout_milliseconds := 15000
        )$$,
      launcher_url, body::text, service_key
    )
  ) INTO job_id;

  RETURN job_id;
END;
$fn$ LANGUAGE plpgsql SECURITY DEFINER;

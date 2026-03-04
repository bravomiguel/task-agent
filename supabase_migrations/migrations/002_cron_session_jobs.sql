-- Create a cron job that fires via the cron-launcher Edge Function.
-- The Edge Function handles the two-step LangGraph create-thread + start-run
-- that pg_cron cannot do in a single net.http_post call. It injects a tag and
-- delivery instructions into the agent message.
-- session_type defaults to 'cron'; use 'heartbeat' for heartbeat jobs.
--
-- Two-step: schedule first to get job_id, then rebuild the body with job_id
-- included and alter the job command so the Edge Function receives it.

-- Drop older signatures to avoid PostgREST PGRST203 ambiguity
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text, boolean);

CREATE OR REPLACE FUNCTION create_cron_session_job(
  job_name text,
  schedule_expr text,
  input_message text,
  session_type text DEFAULT 'cron',
  once boolean DEFAULT false,
  schedule_type text DEFAULT 'cron'
) RETURNS bigint AS $fn$
DECLARE
  job_id bigint;
  launcher_url text;
  service_key text;
  body jsonb;
  final_command text;
BEGIN
  launcher_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'cron_launcher_url');
  service_key  := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_service_key');

  -- Step 1: schedule with a placeholder body to obtain job_id
  SELECT cron.schedule(
    job_name,
    schedule_expr,
    'SELECT 1'  -- placeholder, replaced immediately below
  ) INTO job_id;

  -- Step 2: build the real body including job_id
  body := jsonb_build_object(
    'job_name',       job_name,
    'input_message',  input_message,
    'session_type',   session_type,
    'once',           once,
    'job_id',         job_id,
    'schedule_type',  schedule_type
  );

  final_command := format(
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
  );

  -- Step 3: update the job with the real command
  PERFORM cron.alter_job(job_id, command := final_command);

  RETURN job_id;
END;
$fn$ LANGUAGE plpgsql SECURITY DEFINER;

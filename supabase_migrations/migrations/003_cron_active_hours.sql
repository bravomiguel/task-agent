-- Add active_hours support to heartbeat cron jobs.
-- Active hours (timezone, start, end) are stored in the cron job's POST body
-- and checked by the cron-launcher edge function before creating a thread.
--
-- update_agent_cron_body: rebuilds the full pg_cron command with a new JSON body.
-- create_cron_session_job: now accepts optional timezone and active_hours params.

-- New function: update the POST body of an existing cron job
CREATE OR REPLACE FUNCTION update_agent_cron_body(
  job_id bigint,
  new_body jsonb
) RETURNS void AS $fn$
DECLARE
  launcher_url text;
  service_key text;
  final_command text;
BEGIN
  launcher_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'cron_launcher_url');
  service_key  := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'supabase_service_key');

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
    launcher_url, new_body::text, service_key
  );

  PERFORM cron.alter_job(job_id, command := final_command);
END;
$fn$ LANGUAGE plpgsql SECURITY DEFINER;

-- Drop older signatures to avoid PostgREST PGRST203 ambiguity
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text, boolean);
DROP FUNCTION IF EXISTS create_cron_session_job(text, text, text, text, boolean, text);

-- Recreate with optional timezone and active_hours params
CREATE OR REPLACE FUNCTION create_cron_session_job(
  job_name text,
  schedule_expr text,
  input_message text,
  session_type text DEFAULT 'cron',
  once boolean DEFAULT false,
  schedule_type text DEFAULT 'cron',
  timezone text DEFAULT NULL,
  active_hours_start text DEFAULT NULL,
  active_hours_end text DEFAULT NULL
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

  -- Add active hours fields if provided
  IF timezone IS NOT NULL THEN
    body := body || jsonb_build_object('timezone', timezone);
  END IF;
  IF active_hours_start IS NOT NULL THEN
    body := body || jsonb_build_object('active_hours_start', active_hours_start);
  END IF;
  IF active_hours_end IS NOT NULL THEN
    body := body || jsonb_build_object('active_hours_end', active_hours_end);
  END IF;

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

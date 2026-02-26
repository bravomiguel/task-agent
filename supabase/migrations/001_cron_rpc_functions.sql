-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;

-- Create a cron job that triggers a LangGraph agent run
CREATE OR REPLACE FUNCTION create_agent_cron(
  job_name text,
  schedule_expr text,
  thread_id text,
  user_message text
) RETURNS bigint AS $$
DECLARE
  job_id bigint;
  api_url text;
  run_url text;
  body jsonb;
BEGIN
  api_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'langgraph_api_url');
  run_url := api_url || '/threads/' || thread_id || '/runs';
  body := jsonb_build_object(
    'assistant_id', 'agent',
    'input', jsonb_build_object(
      'messages', jsonb_build_array(
        jsonb_build_object('role', 'user', 'content', user_message)
      )
    )
  );

  SELECT cron.schedule(
    job_name,
    schedule_expr,
    format(
      'SELECT net.http_post(url := %L, body := %L::jsonb, headers := ''{"Content-Type":"application/json"}''::jsonb, timeout_milliseconds := 10000)',
      run_url, body::text
    )
  ) INTO job_id;

  RETURN job_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- List all cron jobs
CREATE OR REPLACE FUNCTION list_agent_crons()
RETURNS TABLE(jobid bigint, jobname text, schedule text, active boolean, command text) AS $$
  SELECT jobid, jobname, schedule, active, command FROM cron.job ORDER BY jobid;
$$ LANGUAGE sql SECURITY DEFINER;

-- Update a cron job's schedule or active status
CREATE OR REPLACE FUNCTION update_agent_cron(
  job_id bigint,
  new_schedule text DEFAULT NULL,
  new_active boolean DEFAULT NULL
) RETURNS void AS $$
BEGIN
  IF new_schedule IS NOT NULL THEN
    PERFORM cron.alter_job(job_id, schedule := new_schedule);
  END IF;
  IF new_active IS NOT NULL THEN
    PERFORM cron.alter_job(job_id, active := new_active);
  END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Delete a cron job by name
CREATE OR REPLACE FUNCTION delete_agent_cron(job_name text)
RETURNS boolean AS $$
  SELECT cron.unschedule(job_name);
$$ LANGUAGE sql SECURITY DEFINER;

-- Run a cron job immediately (triggers the HTTP POST now)
CREATE OR REPLACE FUNCTION run_agent_cron(job_id bigint)
RETURNS void AS $$
DECLARE
  cmd text;
BEGIN
  SELECT command INTO cmd FROM cron.job WHERE jobid = job_id;
  IF cmd IS NULL THEN
    RAISE EXCEPTION 'Job % not found', job_id;
  END IF;
  EXECUTE cmd;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Get cron job run history
CREATE OR REPLACE FUNCTION get_agent_cron_runs(
  p_job_id bigint,
  p_limit int DEFAULT 20
)
RETURNS TABLE(
  runid bigint,
  jobid bigint,
  status text,
  return_message text,
  start_time timestamptz,
  end_time timestamptz
) AS $$
  SELECT runid, jobid, status, return_message, start_time, end_time
  FROM cron.job_run_details
  WHERE jobid = p_job_id
  ORDER BY start_time DESC
  LIMIT p_limit;
$$ LANGUAGE sql SECURITY DEFINER;

-- Get cron scheduler status
CREATE OR REPLACE FUNCTION get_cron_status()
RETURNS TABLE(total_jobs bigint, active_jobs bigint, inactive_jobs bigint) AS $$
  SELECT
    count(*) AS total_jobs,
    count(*) FILTER (WHERE active) AS active_jobs,
    count(*) FILTER (WHERE NOT active) AS inactive_jobs
  FROM cron.job;
$$ LANGUAGE sql SECURITY DEFINER;

-- Wake: trigger an immediate agent run with custom text (for heartbeat-now)
CREATE OR REPLACE FUNCTION wake_agent(
  thread_id text,
  wake_text text
) RETURNS bigint AS $$
DECLARE
  api_url text;
  run_url text;
  body jsonb;
  request_id bigint;
BEGIN
  api_url := (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = 'langgraph_api_url');
  run_url := api_url || '/threads/' || thread_id || '/runs';
  body := jsonb_build_object(
    'assistant_id', 'agent',
    'input', jsonb_build_object(
      'messages', jsonb_build_array(
        jsonb_build_object('role', 'user', 'content', wake_text)
      )
    )
  );

  SELECT net.http_post(
    url := run_url,
    body := body,
    headers := '{"Content-Type":"application/json"}'::jsonb,
    timeout_milliseconds := 10000
  ) INTO request_id;

  RETURN request_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

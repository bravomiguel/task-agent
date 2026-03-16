-- Migration 008: Meetings table + rename memory_chunks source values
--
-- 1. Create meetings table for storing meeting data from Electron app
-- 2. Rename source values in memory_chunks: "sessions" → "session-transcripts"

-- ---------------------------------------------------------------------------
-- Meetings table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meetings (
  id text PRIMARY KEY,
  user_id text NOT NULL,
  title text NOT NULL,
  transcript text,
  duration integer,
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  source text NOT NULL DEFAULT 'calendar',
  calendar_event_id text,
  calendar_email text,
  meeting_url text,
  meeting_platform text,
  attendees jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_meetings_user_id ON meetings (user_id);
CREATE INDEX IF NOT EXISTS idx_meetings_started_at ON meetings (started_at DESC);

-- ---------------------------------------------------------------------------
-- Rename memory_chunks source values for consistency
-- ---------------------------------------------------------------------------

UPDATE memory_chunks SET source = 'session-transcripts' WHERE source = 'sessions';

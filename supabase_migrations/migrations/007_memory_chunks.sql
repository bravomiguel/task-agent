-- Migration 007: Memory chunks table with pgvector + full-text search
--
-- Replaces LanceDB-on-volume with Supabase pgvector for durable,
-- crash-safe semantic memory indexing.

-- Enable pgvector extension (Supabase installs into extensions schema)
create extension if not exists vector with schema extensions;

-- Memory chunks table
create table if not exists memory_chunks (
  id bigint generated always as identity primary key,
  chunk_id text not null unique,        -- "{path}::{index}"
  path text not null,                    -- source file path
  source text not null default 'memory', -- "memory" or "sessions"
  start_line int not null,
  end_line int not null,
  doc_hash text not null,                -- MD5(mtime:size) for incremental sync
  text text not null,
  embedding extensions.vector(1536),     -- text-embedding-3-small
  fts tsvector generated always as (to_tsvector('english', text)) stored
);

-- Indexes
create index if not exists idx_memory_chunks_path on memory_chunks (path);
create index if not exists idx_memory_chunks_source on memory_chunks (source);
create index if not exists idx_memory_chunks_doc_hash on memory_chunks (path, doc_hash);
create index if not exists idx_memory_chunks_fts on memory_chunks using gin (fts);
create index if not exists idx_memory_chunks_embedding on memory_chunks
  using hnsw (embedding vector_cosine_ops);


-- ---------------------------------------------------------------------------
-- RPC: Hybrid search (vector + FTS with linear combination scoring)
-- ---------------------------------------------------------------------------

create or replace function search_memory_chunks(
  query_embedding extensions.vector(1536),
  query_text text,
  match_count int default 6,
  min_score float default 0.35,
  source_filter text default null,
  vector_weight float default 0.7,
  text_weight float default 0.3
)
returns table (
  chunk_id text,
  path text,
  source text,
  start_line int,
  end_line int,
  text text,
  score float
)
language plpgsql
as $$
declare
  candidate_count int := match_count * 4;
  total_weight float := vector_weight + text_weight;
  norm_vw float;
  norm_tw float;
begin
  -- Normalize weights
  if total_weight > 0 then
    norm_vw := vector_weight / total_weight;
    norm_tw := text_weight / total_weight;
  else
    norm_vw := 0.7;
    norm_tw := 0.3;
  end if;

  return query
  with
  -- Vector similarity: 1 - cosine_distance
  -- <=> returns cosine distance in [0, 2], so similarity = 1 - distance ∈ [-1, 1]
  vec as (
    select
      mc.chunk_id,
      (1.0 - (mc.embedding <=> query_embedding))::float as vector_score
    from memory_chunks mc
    where (source_filter is null or mc.source = source_filter)
    order by mc.embedding <=> query_embedding
    limit candidate_count
  ),
  -- FTS: rank-based scoring 1/(1+rank) to match prior behavior
  fts as (
    select
      mc.chunk_id,
      (1.0 / (1.0 + row_number() over (order by ts_rank_cd(mc.fts, plainto_tsquery('english', query_text)) desc) - 1))::float as text_score
    from memory_chunks mc
    where
      mc.fts @@ plainto_tsquery('english', query_text)
      and (source_filter is null or mc.source = source_filter)
    order by ts_rank_cd(mc.fts, plainto_tsquery('english', query_text)) desc
    limit candidate_count
  ),
  -- Merge by chunk_id (union)
  -- When a chunk appears in both modalities, blend scores with weights.
  -- When only one modality matched, use that score directly (not penalized).
  merged as (
    select
      coalesce(v.chunk_id, f.chunk_id) as chunk_id,
      (case
        when v.chunk_id is not null and f.chunk_id is not null then
          norm_vw * v.vector_score + norm_tw * f.text_score
        when v.chunk_id is not null then
          v.vector_score
        else
          f.text_score
      end)::float as score
    from vec v
    full outer join fts f on v.chunk_id = f.chunk_id
  )
  select
    mc.chunk_id,
    mc.path,
    mc.source,
    mc.start_line,
    mc.end_line,
    mc.text,
    m.score
  from merged m
  join memory_chunks mc on mc.chunk_id = m.chunk_id
  where m.score >= min_score
  order by m.score desc
  limit match_count;
end;
$$;


-- ---------------------------------------------------------------------------
-- RPC: Delete chunks by path
-- ---------------------------------------------------------------------------

create or replace function delete_memory_chunks_by_path(target_path text)
returns void
language sql
as $$
  delete from memory_chunks where path = target_path;
$$;


-- ---------------------------------------------------------------------------
-- RPC: Get indexed file metadata (path → doc_hash) for incremental sync
-- ---------------------------------------------------------------------------

create or replace function get_memory_index_meta()
returns table (path text, doc_hash text)
language sql
as $$
  select distinct on (path) path, doc_hash from memory_chunks order by path;
$$;

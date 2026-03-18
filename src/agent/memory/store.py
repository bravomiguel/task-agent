"""Supabase pgvector memory store — sync and hybrid search.

Replaces LanceDB-on-volume with Supabase pgvector for durable, crash-safe
semantic memory indexing.  All operations run in the orchestrator process
(no sandbox exec needed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import openai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MEMORY_DIR = "/mnt/memory"
SESSIONS_DIR = "/mnt/session-transcripts"
MEETINGS_DIR = "/mnt/meeting-transcripts"
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 1536

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 80
CHARS_PER_TOKEN = 4  # rough approximation

# Hybrid search defaults
VECTOR_WEIGHT = 0.7
TEXT_WEIGHT = 0.3
MIN_SCORE = 0.35


# ---------------------------------------------------------------------------
# Supabase client (lazy)
# ---------------------------------------------------------------------------

_supabase_client = None


def _get_supabase():
    """Lazy-init Supabase client from env vars."""
    global _supabase_client
    if _supabase_client is None:
        from supabase import create_client

        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _supabase_client = create_client(url, key)
    return _supabase_client


# ---------------------------------------------------------------------------
# OpenAI embeddings
# ---------------------------------------------------------------------------

_openai_client = None


def _get_openai() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI()
    return _openai_client


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts via OpenAI API.

    Handles batching for large inputs (max 2048 per API call).
    """
    client = _get_openai()
    all_embeddings: list[list[float]] = []
    batch_size = 2048

    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=batch,
        )
        all_embeddings.extend([d.embedding for d in response.data])

    return all_embeddings


def _embed_query(text: str) -> list[float]:
    """Embed a single query string."""
    return _embed_texts([text])[0]


# ---------------------------------------------------------------------------
# Markdown chunking (preserved from _sandbox_script.py)
# ---------------------------------------------------------------------------


def chunk_markdown(
    content: str,
    path: str,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """Split markdown into overlapping chunks with line-number tracking."""
    lines = content.split("\n")
    if not lines:
        return []

    chunk_chars = chunk_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap * CHARS_PER_TOKEN

    chunks: list[dict] = []
    buf: list[str] = []
    buf_chars = 0
    chunk_start = 1  # 1-indexed

    for line_num, line in enumerate(lines, start=1):
        buf.append(line)
        buf_chars += len(line) + 1  # +1 for newline

        if buf_chars >= chunk_chars:
            text = "\n".join(buf).strip()
            if text:
                chunks.append(
                    {
                        "text": text,
                        "path": path,
                        "start_line": chunk_start,
                        "end_line": line_num,
                    }
                )

            # Keep last overlap_chars worth of lines for the next chunk
            overlap_buf: list[str] = []
            overlap_count = 0
            for ol in reversed(buf):
                overlap_count += len(ol) + 1
                overlap_buf.insert(0, ol)
                if overlap_count >= overlap_chars:
                    break

            buf = overlap_buf
            buf_chars = overlap_count
            chunk_start = line_num - len(overlap_buf) + 1

    # Final partial chunk
    text = "\n".join(buf).strip()
    if text:
        chunks.append(
            {
                "text": text,
                "path": path,
                "start_line": chunk_start,
                "end_line": len(lines),
            }
        )

    return chunks


def classify_source(path: str) -> str:
    """Derive a source label from the file path."""
    if SESSIONS_DIR in path:
        return "session-transcripts"
    if MEETINGS_DIR in path:
        return "meeting-transcripts"
    return "memory"


# ---------------------------------------------------------------------------
# Sync: incremental index of memory files → Supabase
# ---------------------------------------------------------------------------


def get_indexed_meta() -> dict[str, str]:
    """Fetch path → doc_hash mapping from Supabase for incremental sync."""
    try:
        sb = _get_supabase()
        result = sb.rpc("get_memory_index_meta").execute()
        return {row["path"]: row["doc_hash"] for row in (result.data or [])}
    except Exception as exc:
        logger.warning("[MemoryStore] failed to fetch index meta: %s", exc)
        return {}


def delete_chunks_by_path(path: str) -> None:
    """Delete all chunks for a given file path."""
    try:
        sb = _get_supabase()
        sb.rpc("delete_memory_chunks_by_path", {"target_path": path}).execute()
    except Exception as exc:
        logger.warning("[MemoryStore] failed to delete chunks for %s: %s", path, exc)


def upsert_chunks(chunks: list[dict], embeddings: list[list[float]]) -> None:
    """Upsert chunks with embeddings into Supabase.

    Uses batch upsert on the memory_chunks table.
    """
    sb = _get_supabase()
    rows = []
    for chunk, embedding in zip(chunks, embeddings):
        rows.append(
            {
                "chunk_id": chunk["chunk_id"],
                "path": chunk["path"],
                "source": chunk["source"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "doc_hash": chunk["doc_hash"],
                "text": chunk["text"],
                "embedding": embedding,
            }
        )

    # Batch upsert (Supabase handles conflict on chunk_id)
    batch_size = 100
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        sb.table("memory_chunks").upsert(batch, on_conflict="chunk_id").execute()


def sync_memory_chunks(
    files: dict[str, dict],
) -> dict[str, Any]:
    """Incremental sync of memory files to Supabase pgvector.

    Args:
        files: dict of {path: {"content": str, "mtime": float, "size": int}}

    Returns:
        Summary dict with indexed/deleted/unchanged counts.
    """
    if not files:
        return {"status": "ok", "indexed": 0, "deleted": 0, "unchanged": 0}

    # Get current index state
    indexed_meta = get_indexed_meta()

    # Compute which files need indexing
    current_paths = set(files.keys())
    indexed_paths = set(indexed_meta.keys())

    files_to_delete = indexed_paths - current_paths
    files_to_index: list[tuple[str, str]] = []  # (path, meta_hash)

    for path, meta in files.items():
        content_hash = hashlib.md5(
            meta["content"].encode()
        ).hexdigest()
        if path not in indexed_meta or indexed_meta[path] != content_hash:
            files_to_index.append((path, content_hash))

    if not files_to_index and not files_to_delete:
        return {
            "status": "ok",
            "indexed": 0,
            "deleted": 0,
            "unchanged": len(files),
        }

    # Delete removed files
    for path in files_to_delete:
        delete_chunks_by_path(path)

    # Chunk changed files
    all_chunks: list[dict] = []
    for path, meta_hash in files_to_index:
        content = files[path].get("content", "")
        if not content:
            continue

        # Delete old chunks for this file first
        delete_chunks_by_path(path)

        source = classify_source(path)
        chunks = chunk_markdown(content, path)

        for i, chunk in enumerate(chunks):
            chunk["source"] = source
            chunk["doc_hash"] = meta_hash
            chunk["chunk_id"] = f"{path}::{i}"
            all_chunks.append(chunk)

    # Embed and upsert
    if all_chunks:
        texts = [c["text"] for c in all_chunks]
        embeddings = _embed_texts(texts)
        upsert_chunks(all_chunks, embeddings)

    return {
        "status": "ok",
        "indexed": len(files_to_index),
        "deleted": len(files_to_delete),
        "unchanged": len(files) - len(files_to_index),
        "chunks": len(all_chunks),
    }


# ---------------------------------------------------------------------------
# Search: hybrid vector + FTS via Supabase RPC
# ---------------------------------------------------------------------------


def search_memory(
    query: str,
    max_results: int = 6,
    min_score: float = MIN_SCORE,
    source_filter: str | None = None,
) -> dict[str, Any]:
    """Hybrid semantic + full-text search over memory chunks.

    Returns dict matching the previous LanceDB output format:
    {
        "results": [{"path", "startLine", "endLine", "score", "snippet", "source"}, ...],
        "provider": "openai",
        "model": "text-embedding-3-small"
    }
    """
    try:
        query_embedding = _embed_query(query)
    except Exception as exc:
        logger.warning("[MemorySearch] embedding failed: %s", exc)
        return {"results": [], "error": f"Embedding failed: {exc}"}

    try:
        sb = _get_supabase()
        params: dict[str, Any] = {
            "query_embedding": query_embedding,
            "query_text": query,
            "match_count": max_results,
            "min_score": min_score,
            "vector_weight": VECTOR_WEIGHT,
            "text_weight": TEXT_WEIGHT,
        }
        if source_filter:
            params["source_filter"] = source_filter

        result = sb.rpc("search_memory_chunks", params).execute()
        rows = result.data or []
    except Exception as exc:
        logger.warning("[MemorySearch] search RPC failed: %s", exc)
        return {"results": [], "error": f"Search failed: {exc}"}

    output: list[dict] = []
    for row in rows:
        text = row.get("text", "")
        output.append(
            {
                "path": row["path"],
                "startLine": row["start_line"],
                "endLine": row["end_line"],
                "score": round(float(row["score"]), 4),
                "snippet": text[:700]
                + (
                    " [truncated — read full chunk if relevant]"
                    if len(text) > 700
                    else ""
                ),
                "source": row.get("source", "memory"),
            }
        )

    return {
        "results": output,
        "provider": "openai",
        "model": EMBEDDING_MODEL,
    }

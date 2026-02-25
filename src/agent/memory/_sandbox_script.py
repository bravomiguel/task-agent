#!/usr/bin/env python3
"""Memory index operations — executed inside Modal sandbox.

This script is piped to `python3 -` via sandbox.exec() from the LangGraph
server.  It has two subcommands:

  sync   — Incremental index of /default-user/memory/*.md and
           /default-user/session-transcripts/*.md into LanceDB
  search — Hybrid BM25 + vector search against the index

LanceDB stores its data as immutable Lance flat-files at
/default-user/memory/.lancedb/ on the Modal Volume — no SQLite locking issues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (mirrored in store.py for the orchestrator side)
# ---------------------------------------------------------------------------

DB_PATH = "/default-user/memory/.lancedb"
TABLE_NAME = "memory_chunks"
MEMORY_DIR = "/default-user/memory"
SESSIONS_DIR = "/default-user/session-transcripts"
EMBEDDING_MODEL = "text-embedding-3-small"

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 80
CHARS_PER_TOKEN = 4  # rough approximation

# Hybrid search defaults (mirroring OpenClaw)
VECTOR_WEIGHT = 0.7
TEXT_WEIGHT = 0.3
CANDIDATE_MULTIPLIER = 4
MIN_SCORE = 0.35


# ---------------------------------------------------------------------------
# Markdown chunking
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
                chunks.append({
                    "text": text,
                    "path": path,
                    "start_line": chunk_start,
                    "end_line": line_num,
                })

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
        chunks.append({
            "text": text,
            "path": path,
            "start_line": chunk_start,
            "end_line": len(lines),
        })

    return chunks


def _classify_source(path: str) -> str:
    """Derive a source label from the file path."""
    if path.startswith(SESSIONS_DIR):
        return "sessions"
    return "memory"


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        json.dump({"status": "error", "message": "No OpenAI API key"}, sys.stdout)
        return

    os.environ["OPENAI_API_KEY"] = api_key

    try:
        import lancedb
        from lancedb.embeddings import get_registry
        from lancedb.pydantic import LanceModel, Vector
    except ImportError as exc:
        json.dump({"status": "error", "message": f"Import failed: {exc}"}, sys.stdout)
        return

    embed_fn = get_registry().get("openai").create(name=EMBEDDING_MODEL)

    class MemoryChunk(LanceModel):
        text: str = embed_fn.SourceField()
        vector: Vector(embed_fn.ndims()) = embed_fn.VectorField()  # type: ignore[valid-type]
        path: str
        source: str
        start_line: int
        end_line: int
        doc_hash: str
        chunk_id: str

    # -- connect ----------------------------------------------------------
    db = lancedb.connect(DB_PATH)

    existing = db.list_tables().tables
    if TABLE_NAME in existing:
        table = db.open_table(TABLE_NAME)
    else:
        table = db.create_table(TABLE_NAME, schema=MemoryChunk)

    # -- list memory and session files ------------------------------------
    current_files: dict[str, dict] = {}
    for dir_path in (MEMORY_DIR, SESSIONS_DIR):
        dp = Path(dir_path)
        if not dp.is_dir():
            continue
        for f in sorted(dp.iterdir()):
            if f.name.startswith("."):
                continue  # skip .lancedb/ and hidden files
            if f.is_file() and f.suffix == ".md":
                stat = f.stat()
                current_files[str(f)] = {
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                }

    if not current_files:
        json.dump({"status": "ok", "indexed": 0, "deleted": 0, "unchanged": 0}, sys.stdout)
        return

    # -- get indexed metadata ---------------------------------------------
    indexed_meta: dict[str, str] = {}  # path → doc_hash
    try:
        df = table.to_pandas()
        if not df.empty:
            for path, doc_hash in zip(df["path"], df["doc_hash"]):
                indexed_meta[path] = doc_hash  # dedupes naturally
    except Exception:
        pass  # empty table or schema mismatch

    # -- diff -------------------------------------------------------------
    files_to_index: list[tuple[str, str]] = []  # (path, meta_hash)
    files_to_delete = set(indexed_meta.keys()) - set(current_files.keys())

    for path, meta in current_files.items():
        meta_hash = hashlib.md5(
            f"{meta['mtime']}:{meta['size']}".encode()
        ).hexdigest()
        if path not in indexed_meta or indexed_meta[path] != meta_hash:
            files_to_index.append((path, meta_hash))

    if not files_to_index and not files_to_delete:
        json.dump({
            "status": "ok",
            "indexed": 0,
            "deleted": 0,
            "unchanged": len(current_files),
        }, sys.stdout)
        return

    # -- delete removed files ---------------------------------------------
    for path in files_to_delete:
        try:
            table.delete(f'path = "{path}"')
        except Exception:
            pass

    # -- read, chunk, and collect new data --------------------------------
    all_chunks: list[dict] = []
    for path, meta_hash in files_to_index:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        # Remove old chunks for this file
        try:
            table.delete(f'path = "{path}"')
        except Exception:
            pass

        source = _classify_source(path)
        chunks = chunk_markdown(content, path)

        for i, chunk in enumerate(chunks):
            chunk["source"] = source
            chunk["doc_hash"] = meta_hash
            chunk["chunk_id"] = f"{path}::{i}"
            all_chunks.append(chunk)

    # -- add (triggers auto-embedding) ------------------------------------
    if all_chunks:
        table.add(all_chunks)

    # -- rebuild FTS index ------------------------------------------------
    try:
        table.create_fts_index("text", use_tantivy=True, replace=True)
    except Exception:
        try:
            table.create_fts_index("text", replace=True)
        except Exception:
            pass  # FTS not critical — vector search still works

    json.dump({
        "status": "ok",
        "indexed": len(files_to_index),
        "deleted": len(files_to_delete),
        "unchanged": len(current_files) - len(files_to_index),
        "chunks": len(all_chunks),
    }, sys.stdout)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def _bm25_rank_to_score(rank: int) -> float:
    """Convert BM25 ordinal rank (0-based, lower=better) → (0, 1].

    Mirrors OpenClaw's ``bm25RankToScore``: ``1 / (1 + rank)``.
    """
    return 1.0 / (1.0 + rank)


def _merge_hybrid_results(
    vector_rows: list[dict],
    fts_rows: list[dict],
    vector_weight: float = VECTOR_WEIGHT,
    text_weight: float = TEXT_WEIGHT,
) -> list[dict]:
    """Merge vector and FTS results with linear combination scoring.

    Mirrors OpenClaw's mergeHybridResults():
    - Union by chunk_id
    - Linear combination: vectorWeight * vectorScore + textWeight * textScore
    - Missing modality gets score 0
    """
    # Normalize weights to sum to 1.0
    total = vector_weight + text_weight
    if total > 0:
        vector_weight = vector_weight / total
        text_weight = text_weight / total

    by_id: dict[str, dict] = {}

    for row in vector_rows:
        cid = row["chunk_id"]
        # With cosine distance_type, _distance = 1 - cos_sim ∈ [0, 2]
        by_id[cid] = {
            **row,
            "vector_score": 1.0 - float(row.get("_distance", 1.0)),
            "text_score": 0.0,
        }

    for row in fts_rows:
        cid = row["chunk_id"]
        text_score = float(row.get("_rank_score", 0.0))
        if cid in by_id:
            by_id[cid]["text_score"] = text_score
        else:
            by_id[cid] = {
                **row,
                "vector_score": 0.0,
                "text_score": text_score,
            }

    merged = []
    for entry in by_id.values():
        entry["score"] = (
            vector_weight * entry["vector_score"]
            + text_weight * entry["text_score"]
        )
        merged.append(entry)

    merged.sort(key=lambda x: x["score"], reverse=True)
    return merged


def cmd_search(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        os.environ["OPENAI_API_KEY"] = api_key

    try:
        import lancedb
    except ImportError as exc:
        json.dump({"results": [], "error": f"Import failed: {exc}"}, sys.stdout)
        return

    db = lancedb.connect(DB_PATH)

    if TABLE_NAME not in db.list_tables().tables:
        json.dump({"results": [], "message": "No memory index found"}, sys.stdout)
        return

    table = db.open_table(TABLE_NAME)
    query = args.query
    max_results = args.max_results
    min_score = args.min_score
    source_filter = args.source
    candidates = max_results * CANDIDATE_MULTIPLIER

    # Build optional where clause for source filtering
    where_clause = f'source = "{source_filter}"' if source_filter else None

    # -- Vector search (cosine distance) -----------------------------------
    vector_rows: list[dict] = []
    try:
        q = (
            table.search(query, query_type="vector")
            .distance_type("cosine")
            .limit(candidates)
        )
        if where_clause:
            q = q.where(where_clause)
        vdf = q.to_pandas()
        if not vdf.empty:
            vector_rows = vdf.to_dict("records")
    except Exception as exc:
        import traceback
        print(f"[search] vector search failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    # -- FTS search (rank-based scoring) ----------------------------------
    fts_rows: list[dict] = []
    try:
        q = table.search(query, query_type="fts").limit(candidates)
        if where_clause:
            q = q.where(where_clause)
        fdf = q.to_pandas()
        if not fdf.empty:
            # FTS results are ordered by BM25 relevance (best first).
            # Convert ordinal rank → score: 1/(1+rank), matching OpenClaw.
            rows = fdf.to_dict("records")
            for rank, row in enumerate(rows):
                row["_rank_score"] = _bm25_rank_to_score(rank)
            fts_rows = rows
    except Exception as exc:
        import traceback
        print(f"[search] FTS search failed: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    if not vector_rows and not fts_rows:
        json.dump({"results": []}, sys.stdout)
        return

    # -- Merge and rank ---------------------------------------------------
    if vector_rows and fts_rows:
        merged = _merge_hybrid_results(vector_rows, fts_rows)
    elif vector_rows:
        # Cosine distance: score = 1 - distance = cosine similarity
        merged = [
            {**r, "score": 1.0 - float(r.get("_distance", 1.0))}
            for r in vector_rows
        ]
        merged.sort(key=lambda x: x["score"], reverse=True)
    else:
        # Rank-based: _rank_score already computed above
        merged = [
            {**r, "score": float(r.get("_rank_score", 0.0))}
            for r in fts_rows
        ]
        merged.sort(key=lambda x: x["score"], reverse=True)

    # -- Filter and format ------------------------------------------------
    output: list[dict] = []
    for entry in merged:
        if entry["score"] < min_score:
            continue
        if len(output) >= max_results:
            break
        output.append({
            "path": entry["path"],
            "startLine": int(entry["start_line"]),
            "endLine": int(entry["end_line"]),
            "score": round(entry["score"], 4),
            "snippet": str(entry["text"])[:700] + (" [truncated — use read_file to see full content]" if len(str(entry["text"])) > 700 else ""),
            "source": entry.get("source", "memory"),
        })

    json.dump({
        "results": output,
        "provider": "openai",
        "model": EMBEDDING_MODEL,
    }, sys.stdout)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Memory index operations")
    sub = parser.add_subparsers(dest="command")

    p_sync = sub.add_parser("sync", help="Incremental index sync")
    p_sync.add_argument("--api-key", default="")

    p_search = sub.add_parser("search", help="Hybrid search")
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--max-results", type=int, default=6)
    p_search.add_argument("--min-score", type=float, default=MIN_SCORE)
    p_search.add_argument("--source", default="", help="Filter by source label (e.g. 'sessions', 'memory')")
    p_search.add_argument("--api-key", default="")

    args = parser.parse_args()

    if args.command == "sync":
        cmd_sync(args)
    elif args.command == "search":
        cmd_search(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

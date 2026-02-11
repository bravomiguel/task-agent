#!/usr/bin/env python3
"""Memory index operations — executed inside Modal sandbox.

This script is piped to `python3 -` via sandbox.exec() from the LangGraph
server.  It has two subcommands:

  sync   — Incremental index of /default-user/memory/*.md into LanceDB
  search — Hybrid BM25 + vector search against the index

LanceDB stores its data as immutable Lance flat-files at
/default-user/memory/.lancedb/ on the Modal Volume — no SQLite locking issues.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants (mirrored in store.py for the orchestrator side)
# ---------------------------------------------------------------------------

DB_PATH = "/default-user/memory/.lancedb"
TABLE_NAME = "memory_chunks"
MEMORY_DIR = "/default-user/memory"
EMBEDDING_MODEL = "text-embedding-3-small"

CHUNK_TOKENS = 400
CHUNK_OVERLAP = 50
CHARS_PER_TOKEN = 4  # rough approximation


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


def _classify_source(filename: str) -> str:
    """Derive a source label from the filename."""
    if filename == "MEMORY.md":
        return "long-term"
    if re.match(r"\d{4}-\d{2}-\d{2}\.md$", filename):
        return "daily-log"
    if re.match(r"\d{4}-\d{2}-\d{2}-.+\.md$", filename):
        return "session-archive"
    return "note"


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

    # -- list memory files ------------------------------------------------
    memory_path = Path(MEMORY_DIR)
    current_files: dict[str, dict] = {}
    for f in sorted(memory_path.iterdir()):
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

        filename = Path(path).name
        source = _classify_source(filename)
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

    # Try hybrid search (BM25 + vector), fall back to vector-only
    results_df = None
    try:
        from lancedb.rerankers import LinearCombinationReranker

        reranker = LinearCombinationReranker(weight=0.7)
        results_df = (
            table.search(query, query_type="hybrid")
            .rerank(reranker=reranker)
            .limit(max_results)
            .to_pandas()
        )
    except Exception:
        try:
            results_df = (
                table.search(query)
                .limit(max_results)
                .to_pandas()
            )
        except Exception as exc:
            json.dump({"results": [], "error": str(exc)}, sys.stdout)
            return

    if results_df is None or results_df.empty:
        json.dump({"results": []}, sys.stdout)
        return

    output: list[dict] = []
    for _, row in results_df.iterrows():
        if "_relevance_score" in row:
            score = float(row["_relevance_score"])
        elif "_distance" in row:
            score = round(1.0 / (1.0 + float(row["_distance"])), 4)
        else:
            score = 0.0

        output.append({
            "path": row["path"],
            "start_line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "score": round(score, 4),
            "snippet": str(row["text"])[:500],
        })

    json.dump({"results": output}, sys.stdout)


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

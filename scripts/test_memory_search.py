"""End-to-end test for Supabase pgvector memory store.

Tests: embedding, upsert, incremental sync, hybrid search (vector + FTS),
source filtering, score thresholds, delete, and cleanup.

Usage:
    python scripts/test_memory_search.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Import store module directly to avoid pulling in the full agent graph
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "store",
    os.path.join(os.path.dirname(__file__), "..", "src", "agent", "memory", "store.py"),
)
store = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(store)

_embed_texts = store._embed_texts
_embed_query = store._embed_query
_get_supabase = store._get_supabase
chunk_markdown = store.chunk_markdown
classify_source = store.classify_source
delete_chunks_by_path = store.delete_chunks_by_path
get_indexed_meta = store.get_indexed_meta
search_memory = store.search_memory
sync_memory_chunks = store.sync_memory_chunks
upsert_chunks = store.upsert_chunks

TEST_PREFIX = "/mnt/memory/_test_"
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {PASS} {name}")
    else:
        failed += 1
        print(f"  {FAIL} {name}{f' — {detail}' if detail else ''}")


def cleanup():
    """Remove all test chunks from Supabase."""
    sb = _get_supabase()
    sb.table("memory_chunks").delete().like("path", f"{TEST_PREFIX}%").execute()


def test_embedding():
    print("\n1. Embeddings")
    emb = _embed_query("hello world")
    check("embed_query returns 1536-dim vector", len(emb) == 1536)
    check("values are floats", isinstance(emb[0], float))

    batch = _embed_texts(["foo", "bar", "baz"])
    check("embed_texts batch returns 3 vectors", len(batch) == 3)
    check("each vector is 1536-dim", all(len(v) == 1536 for v in batch))


def test_chunking():
    print("\n2. Chunking")
    content = "\n".join([f"Line {i}: " + "word " * 50 for i in range(100)])
    chunks = chunk_markdown(content, "/test/file.md")
    check("produces multiple chunks", len(chunks) > 1)
    check("chunks have required keys", all(
        {"text", "path", "start_line", "end_line"} <= set(c.keys()) for c in chunks
    ))
    check("first chunk starts at line 1", chunks[0]["start_line"] == 1)
    check("last chunk ends at line 100", chunks[-1]["end_line"] == 100)
    check("classify_source memory", classify_source("/mnt/memory/foo.md") == "memory")
    check("classify_source sessions", classify_source("/mnt/session-transcripts/x.md") == "sessions")


def test_upsert_and_search():
    print("\n3. Upsert + Search")
    cleanup()

    # Create test chunks
    chunks = [
        {
            "chunk_id": f"{TEST_PREFIX}a.md::0",
            "path": f"{TEST_PREFIX}a.md",
            "source": "memory",
            "start_line": 1,
            "end_line": 5,
            "doc_hash": "abc123",
            "text": "The quick brown fox jumped over the lazy dog in the garden.",
        },
        {
            "chunk_id": f"{TEST_PREFIX}b.md::0",
            "path": f"{TEST_PREFIX}b.md",
            "source": "sessions",
            "start_line": 1,
            "end_line": 3,
            "doc_hash": "def456",
            "text": "Miguel asked about setting up Dropbox and Box cloud storage integrations.",
        },
        {
            "chunk_id": f"{TEST_PREFIX}c.md::0",
            "path": f"{TEST_PREFIX}c.md",
            "source": "memory",
            "start_line": 1,
            "end_line": 4,
            "doc_hash": "ghi789",
            "text": "Python programming best practices: use type hints, write tests, handle errors gracefully.",
        },
    ]

    texts = [c["text"] for c in chunks]
    embeddings = _embed_texts(texts)
    upsert_chunks(chunks, embeddings)

    # Verify upsert
    meta = get_indexed_meta()
    test_paths = {k: v for k, v in meta.items() if k.startswith(TEST_PREFIX)}
    check("3 test files in index meta", len(test_paths) == 3)

    # Vector search — "cloud storage Dropbox" should match chunk b
    result = search_memory("cloud storage Dropbox", max_results=3, min_score=0.0)
    check("search returns results", len(result["results"]) > 0)
    check("provider is openai", result.get("provider") == "openai")
    top = result["results"][0]
    check("top result has score", "score" in top and top["score"] > 0)
    check("top result has path", "path" in top)
    check("top result has snippet", "snippet" in top)
    check("top result has startLine/endLine", "startLine" in top and "endLine" in top)

    # FTS search — keyword match
    result2 = search_memory("Dropbox Box", max_results=3, min_score=0.0)
    paths_found = [r["path"] for r in result2["results"]]
    check("FTS finds Dropbox chunk", f"{TEST_PREFIX}b.md" in paths_found,
          f"got: {paths_found}")

    # Source filtering
    result3 = search_memory("cloud storage", max_results=10, min_score=0.0, source_filter="sessions")
    sources = [r["source"] for r in result3["results"]]
    check("source filter returns only sessions", all(s == "sessions" for s in sources),
          f"got: {sources}")

    # Score threshold
    result4 = search_memory("cloud storage", max_results=10, min_score=0.99)
    check("high min_score filters results", len(result4["results"]) == 0,
          f"got {len(result4['results'])} results")


def test_sync_incremental():
    print("\n4. Incremental sync")
    cleanup()

    files = {
        f"{TEST_PREFIX}sync1.md": {
            "mtime": 1000.0,
            "size": 50,
            "content": "This is a test document about machine learning and neural networks.",
        },
        f"{TEST_PREFIX}sync2.md": {
            "mtime": 2000.0,
            "size": 60,
            "content": "Another document discussing database optimization and indexing strategies.",
        },
    }

    # First sync — should index both
    r1 = sync_memory_chunks(files)
    check("first sync indexes 2 files", r1.get("indexed") == 2, f"got: {r1}")
    check("first sync deletes 0", r1.get("deleted") == 0)

    # Second sync (same files) — should skip both
    r2 = sync_memory_chunks(files)
    check("second sync indexes 0 (no changes)", r2.get("indexed") == 0, f"got: {r2}")
    check("second sync unchanged=2", r2.get("unchanged") == 2, f"got: {r2}")

    # Modify one file
    files[f"{TEST_PREFIX}sync1.md"]["mtime"] = 3000.0
    files[f"{TEST_PREFIX}sync1.md"]["content"] = "Updated content about deep learning transformers."
    r3 = sync_memory_chunks(files)
    check("modified file re-indexed", r3.get("indexed") == 1, f"got: {r3}")

    # Remove one file
    del files[f"{TEST_PREFIX}sync2.md"]
    r4 = sync_memory_chunks(files)
    check("removed file deleted from index", r4.get("deleted") == 1, f"got: {r4}")

    # Search for the updated content
    result = search_memory("deep learning transformers", max_results=5, min_score=0.0)
    paths = [r["path"] for r in result["results"]]
    check("search finds updated content", f"{TEST_PREFIX}sync1.md" in paths,
          f"got: {paths}")


def test_delete():
    print("\n5. Delete")
    cleanup()

    # Insert a chunk, then delete it
    chunks = [{
        "chunk_id": f"{TEST_PREFIX}del.md::0",
        "path": f"{TEST_PREFIX}del.md",
        "source": "memory",
        "start_line": 1,
        "end_line": 2,
        "doc_hash": "xxx",
        "text": "This will be deleted.",
    }]
    embeddings = _embed_texts([c["text"] for c in chunks])
    upsert_chunks(chunks, embeddings)

    meta_before = get_indexed_meta()
    check("chunk exists before delete", f"{TEST_PREFIX}del.md" in meta_before)

    delete_chunks_by_path(f"{TEST_PREFIX}del.md")

    meta_after = get_indexed_meta()
    check("chunk gone after delete", f"{TEST_PREFIX}del.md" not in meta_after)


def main():
    global passed, failed
    print("=" * 60)
    print("Supabase pgvector memory store — end-to-end test")
    print("=" * 60)

    t0 = time.monotonic()
    try:
        test_embedding()
        test_chunking()
        test_upsert_and_search()
        test_sync_incremental()
        test_delete()
    finally:
        cleanup()

    elapsed = time.monotonic() - t0
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed ({elapsed:.1f}s)")
    print(f"{'=' * 60}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

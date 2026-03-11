"""Quick script to inspect the Supabase pgvector memory index."""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from supabase import create_client


def main():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    sb = create_client(url, key)

    # Total chunks
    result = sb.table("memory_chunks").select("id", count="exact").execute()
    total = result.count or 0
    print(f"\nTotal chunks: {total}")

    if total == 0:
        print("No chunks indexed yet.")
        return

    # Chunks by source
    for source in ("memory", "sessions"):
        result = (
            sb.table("memory_chunks")
            .select("id", count="exact")
            .eq("source", source)
            .execute()
        )
        print(f"  [{source}]: {result.count or 0}")

    # Unique paths
    result = sb.rpc("get_memory_index_meta").execute()
    paths = result.data or []
    print(f"\nUnique paths: {len(paths)}")

    for row in sorted(paths, key=lambda r: r["path"])[:15]:
        # Count chunks per path
        cr = (
            sb.table("memory_chunks")
            .select("id", count="exact")
            .eq("path", row["path"])
            .execute()
        )
        print(f"  {row['path']} ({cr.count or 0} chunks, hash={row['doc_hash'][:8]})")

    if len(paths) > 15:
        print(f"  ... and {len(paths) - 15} more")

    # Sample chunks
    print("\nSample chunks (first 3):")
    result = sb.table("memory_chunks").select("*").limit(3).execute()
    for row in result.data or []:
        print(f"\n  [{row['source']}] {row['path']}:{row['start_line']}-{row['end_line']}")
        print(f"  {row['text'][:200]}...")


if __name__ == "__main__":
    main()

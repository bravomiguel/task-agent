"""Quick Modal script to inspect the LanceDB memory index on the volume."""

import modal

app = modal.App("inspect-lancedb")
volume = modal.Volume.from_name("user-default-user", create_if_missing=False)
image = modal.Image.debian_slim(python_version="3.11").pip_install("lancedb", "pandas")


@app.function(volumes={"/default-user": volume}, image=image)
def inspect():
    import lancedb
    import pandas as pd
    from pathlib import Path

    DB_PATH = "/default-user/memory/.lancedb"
    TABLE_NAME = "memory_chunks"

    # Check dirs exist
    for d in ("/default-user/memory", "/default-user/thread-chats"):
        p = Path(d)
        if p.is_dir():
            files = [f.name for f in sorted(p.iterdir()) if f.suffix == ".md"]
            print(f"\n{d}/ — {len(files)} .md files")
            for f in files[:10]:
                print(f"  {f}")
            if len(files) > 10:
                print(f"  ... and {len(files) - 10} more")
        else:
            print(f"\n{d}/ — does not exist")

    # Connect to LanceDB
    if not Path(DB_PATH).exists():
        print(f"\nNo LanceDB at {DB_PATH}")
        return

    db = lancedb.connect(DB_PATH)
    tables = db.list_tables().tables
    print(f"\nLanceDB tables: {tables}")

    if TABLE_NAME not in tables:
        print(f"Table '{TABLE_NAME}' not found")
        return

    table = db.open_table(TABLE_NAME)
    df = table.to_pandas()

    print(f"\nTotal chunks: {len(df)}")
    print(f"\nChunks by source:")
    print(df["source"].value_counts().to_string())

    print(f"\nUnique paths: {df['path'].nunique()}")
    print(f"\nPaths by source:")
    for source in df["source"].unique():
        paths = df[df["source"] == source]["path"].unique()
        print(f"\n  [{source}] — {len(paths)} files:")
        for p in sorted(paths)[:8]:
            count = len(df[df["path"] == p])
            print(f"    {p} ({count} chunks)")
        if len(paths) > 8:
            print(f"    ... and {len(paths) - 8} more")

    print(f"\nSample chunks (first 2 per source):")
    for source in df["source"].unique():
        subset = df[df["source"] == source].head(2)
        for _, row in subset.iterrows():
            print(f"\n  [{row['source']}] {row['path']}:{row['start_line']}-{row['end_line']}")
            print(f"  {row['text'][:200]}...")

    # Detailed sessions dump
    sessions_df = df[df["source"] == "sessions"]
    if not sessions_df.empty:
        print(f"\n{'='*60}")
        print(f"ALL SESSIONS CHUNKS ({len(sessions_df)} total):")
        print(f"{'='*60}")
        for _, row in sessions_df.iterrows():
            print(f"\n  path: {row['path']}")
            print(f"  lines: {row['start_line']}-{row['end_line']}")
            print(f"  chunk_id: {row['chunk_id']}")
            print(f"  doc_hash: {row['doc_hash']}")
            print(f"  text ({len(row['text'])} chars):")
            print(f"  ---")
            for line in row['text'][:500].split('\n'):
                print(f"    {line}")
            if len(row['text']) > 500:
                print(f"    ... ({len(row['text']) - 500} more chars)")
            print(f"  ---")
    else:
        print(f"\nNo sessions chunks found in index!")

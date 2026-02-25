"""Debug: test scoring in _sandbox_script.py search."""

import os
import modal
from pathlib import Path

app = modal.App("test-memory-search")
volume = modal.Volume.from_name("user-default-user", create_if_missing=False)

rclone_image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "lancedb", "tantivy", "openai", "pandas"
)

DEBUG_SCRIPT = """
import json, os, sys
DB_PATH = "/default-user/memory/.lancedb"
TABLE_NAME = "memory_chunks"
QUERY = sys.argv[1]
API_KEY = sys.argv[2]
os.environ["OPENAI_API_KEY"] = API_KEY

import lancedb
db = lancedb.connect(DB_PATH)
table = db.open_table(TABLE_NAME)

# Vector search — show raw distances
print("=== VECTOR ===")
try:
    vdf = table.search(QUERY, query_type="vector").limit(10).to_pandas()
    for _, r in vdf.iterrows():
        dist = r.get("_distance", 0)
        score = 1.0 / (1.0 + float(dist)) if float(dist) >= 0 else 0.0
        print(f"  dist={dist:.4f}  score={score:.4f}  src={r['source']}  {r['path'].split('/')[-1]}")
except Exception as e:
    print(f"  FAILED: {e}")

# FTS search — show raw scores
print("\\n=== FTS ===")
try:
    fdf = table.search(QUERY, query_type="fts").limit(10).to_pandas()
    for _, r in fdf.iterrows():
        raw = float(r.get("_score", 0))
        normalized = raw / (1.0 + raw) if raw > 0 else 0
        print(f"  bm25={raw:.4f}  norm={normalized:.4f}  src={r['source']}  {r['path'].split('/')[-1]}")
except Exception as e:
    print(f"  FAILED: {e}")

# Show what hybrid scores would be
print("\\n=== HYBRID (0.7 vec + 0.3 fts) ===")
try:
    by_id = {}
    for _, r in vdf.iterrows():
        cid = r["chunk_id"]
        d = float(r["_distance"])
        by_id[cid] = {"vec": 1.0 / (1.0 + d) if d >= 0 else 0.0, "fts": 0.0, "path": r["path"].split("/")[-1]}
    for _, r in fdf.iterrows():
        cid = r["chunk_id"]
        fts = float(r["_score"]) / (1.0 + float(r["_score"]))
        if cid in by_id:
            by_id[cid]["fts"] = fts
        else:
            by_id[cid] = {"vec": 0.0, "fts": fts, "path": r["path"].split("/")[-1]}
    for cid, e in sorted(by_id.items(), key=lambda x: -(0.7*x[1]["vec"] + 0.3*x[1]["fts"])):
        hybrid = 0.7 * e["vec"] + 0.3 * e["fts"]
        print(f"  hybrid={hybrid:.4f}  vec={e['vec']:.4f}  fts={e['fts']:.4f}  {e['path']}")
except Exception as e:
    print(f"  FAILED: {e}")
"""


@app.local_entrypoint()
def main(query: str = "cangrejo"):
    api_key = os.environ.get("OPENAI_API_KEY", "")
    sb_app = modal.App.lookup("agent-sandbox", create_if_missing=True)
    sandbox = modal.Sandbox.create(
        app=sb_app, image=rclone_image, workdir="/workspace",
        timeout=120, idle_timeout=60, volumes={"/default-user": volume},
    )
    try:
        p = sandbox.exec("python3", "-c", DEBUG_SCRIPT, query, api_key, timeout=30)
        p.wait()
        print(p.stdout.read())
        stderr = p.stderr.read()
        if stderr:
            print(f"STDERR:\n{stderr}")
    finally:
        sandbox.terminate()

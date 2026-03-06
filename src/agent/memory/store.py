"""LanceDB constants for the memory index.

These are duplicated in _sandbox_script.py (which runs inside the Modal sandbox)
but kept here for use by the orchestrator modules (indexer.py, tools.py).
"""

DB_PATH = "/mnt/memory/.lancedb"
TABLE_NAME = "memory_chunks"
MEMORY_DIR = "/mnt/memory"
EMBEDDING_MODEL = "text-embedding-3-small"

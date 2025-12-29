# POST /threads/search

Search for threads by metadata, status, or state values. Also used to list all threads.

## Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/search",
    body={
        # All fields optional
        "metadata": {"key": "value"},      # Filter by metadata
        "status": "idle",                   # Filter by status
        "limit": 10,                        # Max results (1-1000, default 10)
        "offset": 0,                        # Pagination offset
        "sort_by": "updated_at",           # Sort field
        "sort_order": "desc"               # "asc" or "desc"
    }
)
```

## Request Body Schema

| Field | Type | Description |
|-------|------|-------------|
| `ids` | array[uuid] | Only include these thread IDs |
| `metadata` | object | Filter by thread metadata (exact match) |
| `values` | object | Filter by state values |
| `status` | string | Filter by status: `idle`, `busy`, `interrupted`, `error` |
| `limit` | integer | Max results (1-1000, default 10) |
| `offset` | integer | Pagination offset (default 0) |
| `sort_by` | string | `thread_id`, `status`, `created_at`, `updated_at` |
| `sort_order` | string | `asc` or `desc` |
| `select` | array[string] | Fields to return (default all) |

## Response

Array of Thread objects:

```json
[
  {
    "thread_id": "550e8400-e29b-41d4-a716-446655440000",
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T14:22:00Z",
    "metadata": {
      "source": "email",
      "is_done": false,
      "title": "Budget review request"
    },
    "status": "idle",
    "values": {...}
  }
]
```

## Common Use Cases

### Find threads not marked done

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/search",
    body={
        "metadata": {"is_done": False},
        "status": "idle",
        "limit": 50,
        "sort_by": "updated_at",
        "sort_order": "desc"
    }
)
```

### Find threads from a specific source

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/search",
    body={
        "metadata": {"source": "slack"},
        "limit": 20
    }
)
```

### Get recently active threads

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/search",
    body={
        "sort_by": "updated_at",
        "sort_order": "desc",
        "limit": 10
    }
)
```

### Find threads with specific title keywords

Note: Metadata matching is exact. For partial matches, search multiple threads and filter client-side, or use state values if you stored searchable content there.

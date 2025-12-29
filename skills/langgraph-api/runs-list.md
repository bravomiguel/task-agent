# GET /threads/{thread_id}/runs

List runs for a thread.

## Request

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    params={
        "limit": 10,      # Optional, default 10
        "offset": 0,      # Optional, for pagination
        "status": "success"  # Optional, filter by status
    }
)
```

## Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 10 | Max runs to return |
| `offset` | integer | 0 | Pagination offset |
| `status` | string | - | Filter: `pending`, `error`, `success`, `timeout`, `interrupted` |

## Response

Array of Run objects:

```json
[
  {
    "run_id": "550e8400-e29b-41d4-a716-446655440001",
    "thread_id": "550e8400-e29b-41d4-a716-446655440000",
    "assistant_id": "agent",
    "created_at": "2024-01-15T10:30:00Z",
    "updated_at": "2024-01-15T10:35:00Z",
    "status": "success",
    "metadata": {}
  }
]
```

## Run Status Values

| Status | Description |
|--------|-------------|
| `pending` | Run queued, not yet started |
| `running` | Currently executing |
| `success` | Completed successfully |
| `error` | Failed with error |
| `timeout` | Exceeded time limit |
| `interrupted` | Stopped for human input |

## Example

```python
# Get all successful runs
runs = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    params={"status": "success", "limit": 50}
)
print(f"Thread has {len(runs)} successful runs")
```

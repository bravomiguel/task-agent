# GET /threads/{thread_id}

Get a thread by ID.

## Request

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}"
)
```

## Response

```json
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
  "values": {...},
  "interrupts": {...}
}
```

## Thread Status Values

| Status | Description |
|--------|-------------|
| `idle` | Thread is not running, ready for new runs |
| `busy` | A run is currently executing |
| `interrupted` | Run was interrupted, awaiting input |
| `error` | Last run ended with error |

## Example

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/550e8400-e29b-41d4-a716-446655440000"
)
```

## Error Responses

- `404 Not Found`: Thread does not exist

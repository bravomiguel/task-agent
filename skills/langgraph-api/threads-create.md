# POST /threads

Create a new thread.

## Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={
        # All fields optional
        "thread_id": "optional-custom-uuid",  # Auto-generated if omitted
        "metadata": {
            "source": "email",
            "is_done": False,
            "title": "Task title here"
        }
    }
)
```

## Request Body Schema

| Field | Type | Description |
|-------|------|-------------|
| `thread_id` | uuid | Custom thread ID (auto-generated if omitted) |
| `metadata` | object | Arbitrary metadata to attach |
| `if_exists` | string | `raise` (error on duplicate) or `do_nothing` (return existing) |
| `ttl` | object | Time-to-live config for auto-deletion |

## Response

```json
{
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "metadata": {
    "source": "email",
    "is_done": false,
    "title": "Task title here"
  },
  "status": "idle"
}
```

## Common Patterns

### Create thread for new task

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={
        "metadata": {
            "source": "email",
            "is_done": False,
            "title": "Review Q4 budget proposal"
        }
    }
)
```

### Create thread and immediately run

```python
# Step 1: Create thread
result = http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={"metadata": {"source": "slack", "is_done": False}}
)
thread_id = result["thread_id"]

# Step 2: Kick off run
http_request(
    method="POST",
    url=f"http://127.0.0.1:2024/threads/{thread_id}/runs",
    body={
        "assistant_id": "agent",
        "input": {"messages": [{"role": "user", "content": "Your task here"}]}
    }
)
```

### Idempotent creation

Use `if_exists: "do_nothing"` to safely retry without errors:

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={
        "thread_id": "known-uuid-here",
        "if_exists": "do_nothing",
        "metadata": {"source": "email"}
    }
)
```

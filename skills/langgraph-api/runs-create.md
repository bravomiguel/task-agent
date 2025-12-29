# Thread Runs

Create and manage runs on existing threads.

## POST /threads/{thread_id}/runs

Create a background run on an existing thread. Returns immediately with run ID.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "Your task here"}]
        }
    }
)
```

### Request Body Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `assistant_id` | string | Yes | Assistant/graph ID to run (usually `"agent"`) |
| `input` | object | No | Input to the graph (usually `messages` array) |
| `metadata` | object | No | Metadata to attach to the run |
| `config` | object | No | Runtime configuration overrides |
| `webhook` | string | No | URL to call when run completes |
| `multitask_strategy` | string | No | `reject`, `interrupt`, `rollback`, `enqueue` |

### Response

```json
{
  "run_id": "550e8400-e29b-41d4-a716-446655440001",
  "thread_id": "550e8400-e29b-41d4-a716-446655440000",
  "assistant_id": "agent",
  "created_at": "2024-01-15T10:30:00Z",
  "status": "pending"
}
```

---

## POST /threads/{thread_id}/runs/wait

Create a run and wait for it to complete. Blocks until run finishes.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs/wait",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "Your task here"}]
        }
    }
)
```

### Response

Returns the final output of the graph after completion:

```json
{
  "messages": [...],
  "other_state_keys": ...
}
```

---

## Common Patterns

### Kick off task in new thread

```python
# Step 1: Create thread
thread = http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={
        "metadata": {
            "source": "email",
            "is_done": False,
            "title": "Review Q4 budget"
        }
    }
)

# Step 2: Start run (fire-and-forget)
http_request(
    method="POST",
    url=f"http://127.0.0.1:2024/threads/{thread['thread_id']}/runs",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "Please review the attached budget..."}]
        }
    }
)
```

### Continue existing thread

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{existing_thread_id}/runs",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "Follow-up message here"}]
        }
    }
)
```

### Handle concurrent runs

Use `multitask_strategy` when a run might already be in progress:

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    body={
        "assistant_id": "agent",
        "input": {"messages": [...]},
        "multitask_strategy": "enqueue"  # Queue behind current run
    }
)
```

| Strategy | Behavior |
|----------|----------|
| `reject` | Return 409 if run in progress (default) |
| `interrupt` | Cancel current run, start new one |
| `rollback` | Cancel current run, restore previous state, start new |
| `enqueue` | Queue new run to start after current completes |

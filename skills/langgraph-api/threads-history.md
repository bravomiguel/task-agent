# GET /threads/{thread_id}/history

Get the checkpoint history for a thread. Useful for debugging, time-travel, and understanding thread evolution.

## Request

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/history",
    params={
        "limit": 10  # Optional, default 10
    }
)
```

## Query Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 10 | Max checkpoints to return |
| `before` | string | - | Return checkpoints before this ID |

## Response

Array of ThreadState objects (newest first):

```json
[
  {
    "values": {"messages": [...]},
    "next": [],
    "checkpoint": {"checkpoint_id": "..."},
    "created_at": "2024-01-15T14:22:00Z",
    "parent_checkpoint": {"checkpoint_id": "..."}
  },
  {
    "values": {"messages": [...]},
    "next": ["agent"],
    "checkpoint": {"checkpoint_id": "..."},
    "created_at": "2024-01-15T14:21:00Z",
    "parent_checkpoint": null
  }
]
```

## Common Use Cases

### View conversation progression

```python
history = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/history",
    params={"limit": 20}
)

for i, checkpoint in enumerate(reversed(history)):
    msgs = checkpoint["values"].get("messages", [])
    print(f"Step {i+1}: {len(msgs)} messages, next: {checkpoint.get('next', [])}")
```

### Find checkpoint to resume from

```python
history = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/history"
)

# Find a checkpoint where agent was waiting
for cp in history:
    if "agent" in cp.get("next", []):
        target_checkpoint = cp["checkpoint"]
        break
```

### Resume from specific checkpoint

After finding a checkpoint, create a run from it:

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    body={
        "assistant_id": "agent",
        "checkpoint": {"checkpoint_id": target_checkpoint["checkpoint_id"]}
    }
)
```

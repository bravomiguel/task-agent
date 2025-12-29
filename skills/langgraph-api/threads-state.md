# Thread State

Get or update the current state of a thread.

## GET /threads/{thread_id}/state

Get the latest state (checkpoint) of a thread.

### Request

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/state"
)
```

### Response

```json
{
  "values": {
    "messages": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "other_state_keys": "..."
  },
  "next": ["node_name"],
  "checkpoint": {
    "checkpoint_id": "...",
    "checkpoint_ns": ""
  },
  "metadata": {...},
  "created_at": "2024-01-15T10:30:00Z",
  "parent_checkpoint": {...}
}
```

### Response Fields

| Field | Description |
|-------|-------------|
| `values` | Current state values (messages, custom state) |
| `next` | Next node(s) to execute (empty if complete) |
| `checkpoint` | Checkpoint ID for resuming/branching |
| `metadata` | Checkpoint metadata |
| `created_at` | When this checkpoint was created |

---

## POST /threads/{thread_id}/state

Update thread state directly (without running the graph).

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/state",
    body={
        "values": {
            "messages": [{"role": "user", "content": "Injected message"}]
        }
    }
)
```

### Request Body Schema

| Field | Type | Description |
|-------|------|-------------|
| `values` | object | State values to update/merge |
| `as_node` | string | Node to attribute update to (for reducer logic) |
| `checkpoint` | object | Checkpoint to branch from |

---

## Common Use Cases

### Inspect current messages

```python
state = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/state"
)
messages = state["values"]["messages"]
for msg in messages:
    print(f"{msg['role']}: {msg['content'][:100]}...")
```

### Check if thread is complete

```python
state = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/state"
)
is_complete = len(state.get("next", [])) == 0
```

### Inject a message before running

```python
# Add message to state
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/state",
    body={
        "values": {
            "messages": [{"role": "user", "content": "New input"}]
        },
        "as_node": "__start__"
    }
)

# Then run from updated state
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    body={"assistant_id": "agent"}
)
```

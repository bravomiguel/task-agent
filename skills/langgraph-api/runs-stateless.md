# Stateless Runs

Execute runs without thread persistence. Ideal for one-shot operations like triage decisions.

## POST /runs

Create a background stateless run. Returns run ID immediately.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "One-shot task here"}]
        }
    }
)
```

---

## POST /runs/wait

Create a stateless run and wait for completion. Blocks until done.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs/wait",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{"role": "user", "content": "One-shot task here"}]
        }
    }
)
```

### Request Body Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `assistant_id` | string | Yes | Assistant/graph ID to run |
| `input` | object | No | Input to the graph |
| `metadata` | object | No | Metadata for the run |
| `config` | object | No | Runtime configuration |
| `webhook` | string | No | URL to call on completion |
| `on_completion` | string | No | `delete` (default) or `keep` the temp thread |

### Response

Returns the final graph output:

```json
{
  "messages": [...],
  "other_state_keys": ...
}
```

---

## When to Use Stateless Runs

Use stateless runs for:

- **Triage/routing decisions** - Decide which thread to route to, then execute
- **One-shot transformations** - Process data without needing history
- **Classification tasks** - Categorize input without persistence
- **Validation/checks** - Quick operations that don't need memory

Use thread-based runs when you need:

- Conversation history across multiple interactions
- Human-in-the-loop with interrupts
- Persistent state between runs

---

## Common Patterns

### Triage incoming task

```python
result = http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs/wait",
    body={
        "assistant_id": "agent",
        "input": {
            "messages": [{
                "role": "user",
                "content": """<email>
Subject: Q4 Budget Review
From: finance@company.com
Body: Please review the attached budget proposal...
</email>

Decide: should this become a new task, be added to an existing thread, or ignored?"""
            }]
        }
    }
)
```

### Keep thread for debugging

Use `on_completion: "keep"` to preserve the temp thread:

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs/wait",
    body={
        "assistant_id": "agent",
        "input": {"messages": [...]},
        "on_completion": "keep"  # Don't delete temp thread
    }
)
```

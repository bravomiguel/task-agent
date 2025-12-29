# PATCH /threads/{thread_id}

Update a thread's metadata.

## Request

```python
http_request(
    method="PATCH",
    url="http://127.0.0.1:2024/threads/{thread_id}",
    body={
        "metadata": {"is_done": True}
    }
)
```

## Request Body Schema

| Field | Type | Description |
|-------|------|-------------|
| `metadata` | object | Metadata to merge with existing |

Note: Metadata is merged, not replaced. To remove a key, set it to `null`.

## Response

Updated Thread object.

## Common Use Cases

### Mark thread as done

```python
http_request(
    method="PATCH",
    url="http://127.0.0.1:2024/threads/550e8400-e29b-41d4-a716-446655440000",
    body={
        "metadata": {"is_done": True}
    }
)
```

### Update thread title

```python
http_request(
    method="PATCH",
    url="http://127.0.0.1:2024/threads/550e8400-e29b-41d4-a716-446655440000",
    body={
        "metadata": {"title": "Updated task title"}
    }
)
```

### Add additional metadata

```python
http_request(
    method="PATCH",
    url="http://127.0.0.1:2024/threads/550e8400-e29b-41d4-a716-446655440000",
    body={
        "metadata": {
            "priority": "high",
            "assigned_to": "user@example.com"
        }
    }
)
```

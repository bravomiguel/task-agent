# DELETE /threads/{thread_id}

Delete a thread by ID. This also deletes all runs and checkpoints for the thread.

## Request

```python
http_request(
    method="DELETE",
    url="http://127.0.0.1:2024/threads/{thread_id}"
)
```

## Response

Empty response on success.

## Error Responses

- `404 Not Found`: Thread does not exist

## Example

```python
http_request(
    method="DELETE",
    url="http://127.0.0.1:2024/threads/550e8400-e29b-41d4-a716-446655440000"
)
```

## Caution

This action is irreversible. All thread state, checkpoints, and run history will be permanently deleted.

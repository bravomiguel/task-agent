# POST /threads/{thread_id}/runs/{run_id}/cancel

Cancel a running run.

## Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs/{run_id}/cancel"
)
```

## Response

Empty response on success.

## When to Use

- Run is taking too long
- User requested cancellation
- Need to start a new run with `multitask_strategy: "reject"` (default)

## Example

```python
# List runs to find active one
runs = http_request(
    method="GET",
    url="http://127.0.0.1:2024/threads/{thread_id}/runs",
    params={"status": "running"}
)

if runs:
    # Cancel the running one
    http_request(
        method="POST",
        url=f"http://127.0.0.1:2024/threads/{thread_id}/runs/{runs[0]['run_id']}/cancel"
    )
```

## Bulk Cancel

Cancel all pending/running runs across threads:

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs/cancel",
    body={
        "thread_ids": ["thread-id-1", "thread-id-2"]
    }
)
```

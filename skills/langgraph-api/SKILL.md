---
name: langgraph-api
description: LangGraph API client for managing threads, runs, and state via http_request tool. Use when you need to: (1) search/list threads by metadata or status, (2) create new threads, (3) kick off runs on threads, (4) create stateless one-shot runs, (5) inspect thread state or history, (6) manage long-term memory via Store API. All operations use http_request to http://127.0.0.1:2024.
---

# LangGraph API

API for managing threads, runs, and persistent state. Base URL: `http://127.0.0.1:2024`

## Endpoints Index

### Threads

| Endpoint | File | Description |
|----------|------|-------------|
| `POST /threads/search` | [threads-search.md](threads-search.md) | Search/list threads by metadata, status |
| `POST /threads` | [threads-create.md](threads-create.md) | Create a new thread |
| `GET /threads/{id}` | [threads-get.md](threads-get.md) | Get thread by ID |
| `PATCH /threads/{id}` | [threads-update.md](threads-update.md) | Update thread metadata |
| `DELETE /threads/{id}` | [threads-delete.md](threads-delete.md) | Delete a thread |
| `GET /threads/{id}/state` | [threads-state.md](threads-state.md) | Get/update thread state |
| `GET /threads/{id}/history` | [threads-history.md](threads-history.md) | Get thread checkpoint history |

### Runs

| Endpoint | File | Description |
|----------|------|-------------|
| `POST /threads/{id}/runs` | [runs-create.md](runs-create.md) | Create run on existing thread (background) |
| `POST /threads/{id}/runs/wait` | [runs-create.md](runs-create.md) | Create run and wait for completion |
| `POST /runs` | [runs-stateless.md](runs-stateless.md) | Create stateless run (no persistence) |
| `GET /threads/{id}/runs` | [runs-list.md](runs-list.md) | List runs for a thread |
| `POST /threads/{id}/runs/{run_id}/cancel` | [runs-cancel.md](runs-cancel.md) | Cancel a running run |

### Store (Long-term Memory)

| Endpoint | File | Description |
|----------|------|-------------|
| `PUT /store/items` | [store.md](store.md) | Store/update a key-value item |
| `GET /store/items` | [store.md](store.md) | Retrieve an item by key |
| `POST /store/items/search` | [store.md](store.md) | Search items in namespace |

## Quick Reference

### Search for threads not marked done

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads/search",
    body={"metadata": {"is_done": False}, "limit": 50}
)
```

### Create thread and kick off run

```python
# Create thread
thread = http_request(
    method="POST",
    url="http://127.0.0.1:2024/threads",
    body={"metadata": {"is_done": False, "title": "Task title"}}
)

# Kick off run (background)
http_request(
    method="POST",
    url=f"http://127.0.0.1:2024/threads/{thread['thread_id']}/runs",
    body={
        "assistant_id": "agent",
        "input": {"messages": [{"role": "user", "content": "Your task here"}]}
    }
)
```

### Stateless run (no thread persistence)

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/runs/wait",
    body={
        "assistant_id": "agent",
        "input": {"messages": [{"role": "user", "content": "One-shot task"}]}
    }
)
```

### Mark thread as done

```python
http_request(
    method="PATCH",
    url="http://127.0.0.1:2024/threads/{thread_id}",
    body={"metadata": {"is_done": True}}
)
```

## Thread Metadata Convention

Threads should include these metadata fields:

| Field | Type | Description |
|-------|------|-------------|
| `is_done` | boolean | Whether the task is complete |
| `title` | string | Human-readable task title |

## Thread Status Values

| Status | Description |
|--------|-------------|
| `idle` | Not running, ready for new runs |
| `busy` | Run currently executing |
| `interrupted` | Awaiting human input |
| `error` | Last run failed |

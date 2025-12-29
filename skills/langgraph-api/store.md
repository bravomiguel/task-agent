# Store API

Persistent key-value store for long-term memory accessible from any thread.

## PUT /store/items

Store or update an item.

### Request

```python
http_request(
    method="PUT",
    url="http://127.0.0.1:2024/store/items",
    body={
        "namespace": ["memories", "user-preferences"],
        "key": "theme",
        "value": {"color": "dark", "font_size": 14}
    }
)
```

### Request Body Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `namespace` | array[string] | Yes | Hierarchical namespace path |
| `key` | string | Yes | Item key within namespace |
| `value` | object | Yes | JSON value to store |

---

## GET /store/items

Retrieve a single item.

### Request

```python
http_request(
    method="GET",
    url="http://127.0.0.1:2024/store/items",
    params={
        "namespace": "memories,user-preferences",
        "key": "theme"
    }
)
```

### Response

```json
{
  "namespace": ["memories", "user-preferences"],
  "key": "theme",
  "value": {"color": "dark", "font_size": 14},
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T14:22:00Z"
}
```

---

## DELETE /store/items

Delete an item.

### Request

```python
http_request(
    method="DELETE",
    url="http://127.0.0.1:2024/store/items",
    body={
        "namespace": ["memories", "user-preferences"],
        "key": "theme"
    }
)
```

---

## POST /store/items/search

Search for items within a namespace prefix.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/store/items/search",
    body={
        "namespace_prefix": ["memories"],
        "limit": 100
    }
)
```

### Response

```json
[
  {
    "namespace": ["memories", "user-preferences"],
    "key": "theme",
    "value": {...},
    "created_at": "...",
    "updated_at": "..."
  },
  {
    "namespace": ["memories", "triage"],
    "key": "email-rules",
    "value": {...},
    ...
  }
]
```

---

## POST /store/namespaces

List namespaces with optional filtering.

### Request

```python
http_request(
    method="POST",
    url="http://127.0.0.1:2024/store/namespaces",
    body={
        "prefix": ["memories"]  # Optional
    }
)
```

---

## Common Patterns

### Store triage rules

```python
http_request(
    method="PUT",
    url="http://127.0.0.1:2024/store/items",
    body={
        "namespace": ["triage", "email"],
        "key": "rules",
        "value": {
            "ignore_patterns": ["newsletter@", "noreply@"],
            "priority_senders": ["ceo@company.com", "urgent@"]
        }
    }
)
```

### Retrieve triage rules

```python
rules = http_request(
    method="GET",
    url="http://127.0.0.1:2024/store/items",
    params={
        "namespace": "triage,email",
        "key": "rules"
    }
)
```

### List all items in namespace

```python
items = http_request(
    method="POST",
    url="http://127.0.0.1:2024/store/items/search",
    body={
        "namespace_prefix": ["triage"]
    }
)
```

---
name: trello
description: "Manage Trello boards, lists, and cards via the Trello REST API. Use when: user asks about Trello boards, lists, cards, or wants to manage project tasks in Trello."
---

# Trello Skill

Manage Trello boards, lists, and cards via the Trello REST API.

## Authentication

Before using any Trello commands, set the credentials:

```bash
TRELLO_KEY=$(cat /mnt/auth/trello_key)
TRELLO_TOKEN=$(cat /mnt/auth/trello_token)
```

## Usage

All commands use curl to hit the Trello REST API.

### List boards

```bash
curl -s "https://api.trello.com/1/members/me/boards?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq '.[] | {name, id}'
```

### List lists in a board

```bash
curl -s "https://api.trello.com/1/boards/{boardId}/lists?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq '.[] | {name, id}'
```

### List cards in a list

```bash
curl -s "https://api.trello.com/1/lists/{listId}/cards?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq '.[] | {name, id, desc}'
```

### Create a card

```bash
curl -s -X POST "https://api.trello.com/1/cards?key=$TRELLO_KEY&token=$TRELLO_TOKEN" \
  -d "idList={listId}" \
  -d "name=Card Title" \
  -d "desc=Card description"
```

### Move a card to another list

```bash
curl -s -X PUT "https://api.trello.com/1/cards/{cardId}?key=$TRELLO_KEY&token=$TRELLO_TOKEN" \
  -d "idList={newListId}"
```

### Add a comment to a card

```bash
curl -s -X POST "https://api.trello.com/1/cards/{cardId}/actions/comments?key=$TRELLO_KEY&token=$TRELLO_TOKEN" \
  -d "text=Your comment here"
```

### Archive a card

```bash
curl -s -X PUT "https://api.trello.com/1/cards/{cardId}?key=$TRELLO_KEY&token=$TRELLO_TOKEN" \
  -d "closed=true"
```

## Examples

```bash
# Get all boards
curl -s "https://api.trello.com/1/members/me/boards?key=$TRELLO_KEY&token=$TRELLO_TOKEN&fields=name,id" | jq

# Find a specific board by name
curl -s "https://api.trello.com/1/members/me/boards?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq '.[] | select(.name | contains("Work"))'

# Get all cards on a board
curl -s "https://api.trello.com/1/boards/{boardId}/cards?key=$TRELLO_KEY&token=$TRELLO_TOKEN" | jq '.[] | {name, list: .idList}'
```

## Notes

- Board/List/Card IDs can be found in the Trello URL or via the list commands
- Rate limits: 300 requests per 10 seconds per API key; 100 requests per 10 seconds per token
- `/1/members` endpoints are limited to 100 requests per 900 seconds

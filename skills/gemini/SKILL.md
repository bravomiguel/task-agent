---
name: gemini
description: Gemini CLI for one-shot Q&A, summaries, and generation.
---

# Gemini CLI

Use Gemini in one-shot mode with a positional prompt (avoid interactive mode).

## Authentication

`GEMINI_API_KEY` is pre-configured as an environment variable. No setup needed.

## Quick start

- `gemini "Answer this question..."`
- `gemini --model <name> "Prompt..."`
- `gemini --output-format json "Return JSON"`

## Extensions

- List: `gemini --list-extensions`
- Manage: `gemini extensions <command>`

## Notes

- Avoid interactive mode — use one-shot with a positional prompt.
- Avoid `--yolo` for safety.

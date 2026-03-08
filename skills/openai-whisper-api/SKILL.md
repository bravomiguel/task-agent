---
name: openai-whisper-api
description: Transcribe audio via OpenAI Audio Transcriptions API (Whisper).
---

# OpenAI Whisper API (curl)

Transcribe an audio file via OpenAI's `/v1/audio/transcriptions` endpoint.

## Authentication

`OPENAI_API_KEY` is pre-configured as an environment variable. No setup needed.

## Quick start

```bash
/mnt/skills/openai-whisper-api/scripts/transcribe.sh /path/to/audio.m4a
```

Defaults:

- Model: `whisper-1`
- Output: `<input>.txt`

## Useful flags

```bash
/mnt/skills/openai-whisper-api/scripts/transcribe.sh /path/to/audio.ogg --model whisper-1 --out /tmp/transcript.txt
/mnt/skills/openai-whisper-api/scripts/transcribe.sh /path/to/audio.m4a --language en
/mnt/skills/openai-whisper-api/scripts/transcribe.sh /path/to/audio.m4a --prompt "Speaker names: Peter, Daniel"
/mnt/skills/openai-whisper-api/scripts/transcribe.sh /path/to/audio.m4a --json --out /tmp/transcript.json
```

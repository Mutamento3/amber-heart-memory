# Connect Amber Heart with HTTP hooks

Give this guide to the AI implementing your integration. Your application owns the persona, conversation history, and model calls. Amber Heart supplies session memory, recalls cards before each model call, and records the exchange after receiving the finished reply. Recorded turns feed diaries, card extraction, instant summaries, and nightly memory-use judging.

Generate an access key for the target space in the Amber Heart admin UI. Keep it on your backend. Send each request to the same Amber Heart service using `POST`, `Content-Type: application/json`, and `Authorization: Bearer <space access key>`. Browser apps can call through their own backend. Claude Code can call directly. The request size limit is 16 MiB.

## Custom clients

### 1. At session start

`POST /hooks/session-start`

```json
{"conversation_id": "chat-42"}
```

`conversation_id` is optional and accepts a string of up to 128 characters. Omit it or pass `null` to use the default conversation for the generic API. Session memory comes from the key's space. Use `{}` when supplying no conversation ID.

The example response uses abbreviated text.

```json
{
  "core_memory": "<core_memory>…</core_memory>",
  "recent_notes": "Organized bookmark context…",
  "diary": "<recent_diaries>…</recent_diaries>",
  "summary": "<instant_summaries>…</instant_summaries>",
  "context": "<core_memory>…</core_memory>\n\n<recent_diaries>…</recent_diaries>\n\nOrganized bookmark context…\n\n<instant_summaries>…</instant_summaries>"
}
```

Each section is a ready-to-use string. Empty sections are `""`. The combined `context` joins nonempty core memory, recent diaries, organized bookmarks, and the current business day's instant summaries in that order. Give the model either the sections you need or the combined text. Supply your own persona.

### 2. After the user submits a message, before calling the model

`POST /hooks/user-prompt-submit`

```json
{
  "conversation_id": "chat-42",
  "turn_id": "turn-7",
  "message": "Do you remember which book we picked?",
  "images": [{"url": "https://example.com/book.jpg", "detail": "auto"}]
}
```

Use a unique `turn_id` within each conversation. It can be a string of 1–128 characters or a nonnegative JavaScript safe integer. An integer and its decimal string are equivalent. Use the same ID and message when retrying a submission. Use the same `conversation_id` for submission and completion.

`images` is optional. Each image takes an HTTP(S) or `data:image/…;base64,…` URL and optional `detail` of `auto`, `low`, or `high`. For image-only turns, omit `message`. They skip text recall. If you configure the image-description role, Amber Heart writes the description to the turn in the background. Images and URLs stay out of storage.

```json
{
  "context": "<retrieved_memories>\n…\n[1] 标题：Weekend reading\n日期：2026-06-01\nWe picked a popular science book.\n</retrieved_memories>",
  "cards": [{
    "memory_id": 123,
    "number": 1,
    "title": "Weekend reading",
    "content": "We picked a popular science book.",
    "source_date": "2026-06-01",
    "item_kind": "atomic",
    "experience_mode": null
  }]
}
```

Include `context` in the current model input. Use `cards` to display recall details if desired. A card's title or date can be `null`. Recall shares the chat proxy's rules, deadline (eight seconds by default), and fallback. With no available cards, the response is `{"context":"","cards":[]}` and you can still call the model. Amber Heart stages the user message and returned cards.

### 3. Once the assistant has finished

`POST /hooks/stop`

```json
{"conversation_id": "chat-42", "turn_id": "turn-7", "reply": "We picked a popular science book for the weekend."}
```

```json
{"turn_id": 208, "replaced": false}
```

Send the original client turn ID and the complete visible reply. If you stream the reply, assemble it first. The returned `turn_id` is Amber Heart's internal record ID. To regenerate, send the original client ID and new reply again. The response has `replaced: true`. Repeating the same reply returns the existing record with `replaced: false`.

Unpaired turns expire after 30 minutes. The first complete reply determines the business day for the turn, and replacements keep that day. Completed nightly jobs do not rerun automatically after a replacement.

Errors include `error.code` and `error.message`. Common statuses are `400` for invalid input, `401` for an invalid or revoked key, `404` for a missing or expired turn, `409` for a conflicting submission or pending recall, `413` for an oversized body, and `503` for an unavailable service or a required database upgrade.

## Connect Claude Code directly

Set `AMBER_HEART_ACCESS_KEY` in the environment that launches Claude Code. Merge this configuration into `~/.claude/settings.json` or the project's `.claude/settings.local.json`, replacing the example domain. Use `CLAUDE.md` for persona instructions. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

```json
{
  "hooks": {
    "SessionStart": [
      {"matcher": "startup|clear|compact", "hooks": [{
        "type": "command",
        "command": "curl -sS -X POST -H \"Authorization: Bearer $AMBER_HEART_ACCESS_KEY\" -H 'Content-Type: application/json' --data-binary @- https://memory.example.com/hooks/session-start",
        "timeout": 30
      }]}
    ],
    "UserPromptSubmit": [
      {"hooks": [{
        "type": "http",
        "url": "https://memory.example.com/hooks/user-prompt-submit",
        "headers": {"Authorization": "Bearer $AMBER_HEART_ACCESS_KEY"},
        "allowedEnvVars": ["AMBER_HEART_ACCESS_KEY"],
        "timeout": 30
      }]}
    ],
    "Stop": [
      {"hooks": [{
        "type": "http",
        "url": "https://memory.example.com/hooks/stop",
        "headers": {"Authorization": "Bearer $AMBER_HEART_ACCESS_KEY"},
        "allowedEnvVars": ["AMBER_HEART_ACCESS_KEY"]
      }]}
    ]
  }
}
```

SessionStart only supports `command` hooks, not `http`, so the session-start step forwards the event with `curl`. The `matcher` limits it to new sessions, `/clear` and compaction, so continuing with `--resume` or `--continue` does not inject the fixed memory again.

If your own frontend calls `claude -p` from its backend, this configuration works the same way (tested with `claude -p` and `--resume`). Alternatively, skip the Claude Code hooks and have your backend call the three generic endpoints above before and after each call, which also lets you send images.

Amber Heart reads the event JSON directly. It pairs `UserPromptSubmit.prompt` with `Stop.last_assistant_message` by `session_id` and `prompt_id`. Older clients that omit `prompt_id` use the latest unnumbered submission in that session. The response uses the actual event name and limits `additionalContext` to 10,000 characters. Stop returns empty context so Claude Code can end the turn.

```json
{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"<retrieved_memories>…</retrieved_memories>"}}
```

Claude Code sends text only. Interrupted turns with no Stop expire. Scheduled jobs and subagent reports may also trigger UserPromptSubmit and be recorded as ordinary messages. An unmatched Stop returns empty context.

## Nightly memory-use judging

Configure the **nightly receipt judge** role (`MEMORY_RECEIPT_JUDGE_MODEL`) when using hooks. This job records which cards the assistant used. Without it, hook turns remain unreported and insufficient receipt coverage pauses automatic archiving.

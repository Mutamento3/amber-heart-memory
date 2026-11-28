"""OpenRouter Claude cache breakpoints on the hosted conversation prefix."""

BREAKPOINT_SECTIONS = (
    ("persona", "core_memory"),
    ("diary", "recent_notes"),
    ("summary",),
    ("history",),
)


def mark_claude_cache(body, sections):
    # A client-supplied top-level marker already requests automatic caching.
    # Combining it with four explicit breakpoints can be rejected upstream.
    if not sections or "claude" not in body["model"].casefold() or body.get("cache_control"):
        return
    messages = body["messages"]
    positions = {s["kind"]: range(s["start"], s["end"]) for s in sections}
    for kinds in BREAKPOINT_SECTIONS:
        # Thawed hosted text is a string. Skip an empty section or blank prompt
        # so the breakpoints follow the content that is actually sent.
        last = max(
            (i for kind in kinds for i in positions.get(kind, ())
             if isinstance(messages[i].get("content"), str) and messages[i]["content"].strip()),
            default=None,
        )
        if last is not None:
            message = messages[last]
            messages[last] = dict(message, content=[{
                "type": "text",
                "text": message["content"],
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }])

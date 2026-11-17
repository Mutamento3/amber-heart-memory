"""Bounded frontend diagnostics. Never log provider bodies or credentials."""

import json
from urllib.parse import quote, quote_plus

MAX_ERROR_BYTES = 16384
MAX_ERROR_CHARS = 600


def error_message(value, secret, fallback="上游请求失败"):
    if isinstance(value, dict):
        value = value.get("error", value)
        if isinstance(value, dict):
            value = value.get("message") or value.get("detail") or value.get("code") or fallback
    if not isinstance(value, str):
        value = fallback
    # Redact before shortening, including URL-encoded and JSON-escaped forms.
    secrets = (
        {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}
        if secret
        else set()
    )
    for candidate in sorted(secrets, key=len, reverse=True):
        value = value.replace(candidate, "[REDACTED]")
        # A bounded upstream read could end partway through the credential.
        for length in range(min(len(candidate) - 1, len(value)), 3, -1):
            if value.endswith(candidate[:length]):
                value = value[:-length] + "[REDACTED]"
                break
    value = " ".join(value.split())
    return (value[:MAX_ERROR_CHARS] + "…") if len(value) > MAX_ERROR_CHARS else value or fallback


async def response_error(response, secret, fallback="上游请求失败"):
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk[: max(0, MAX_ERROR_BYTES - len(data))])
        if len(data) >= MAX_ERROR_BYTES:
            break
    text = data.decode("utf-8", errors="replace")
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    return error_message(value, secret, fallback)

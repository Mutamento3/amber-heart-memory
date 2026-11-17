"""Bounded HTTP JSON calls with an absolute deadline and safe error codes."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
from time import monotonic

import httpx

_DEADLINE = ContextVar("memory_model_deadline", default=None)


class ModelCallError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def is_content_filter(error):
    """Read protocol markers only; provider messages can contain private input."""
    if not isinstance(error, dict):
        return False
    metadata = error.get('metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    if any(error.get(key) in ('content_filter', 'content_policy_violation', 'refusal') for key in ('code', 'type')):
        return True
    if metadata.get('error_type') in ('content_policy_violation', 'refusal'):
        return True
    # Older OpenRouter moderation envelopes use numeric 403 + reasons.
    # Budget and permission failures also use 403, so status alone is not enough.
    reasons = metadata.get('reasons')
    return (error.get('code') == 403 and isinstance(reasons, list) and bool(reasons)
            and all(isinstance(reason, str) and bool(reason.strip()) for reason in reasons))


@contextmanager
def call_deadline(deadline):
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


async def post_json(endpoint, key, path, payload, *, role, timeout, transport=None, classify_content_filter=False):
    deadline = _DEADLINE.get()
    remaining = (
        min(timeout, deadline - monotonic()) if deadline is not None else timeout
    )
    if remaining <= 0:
        raise ModelCallError(role + "_timeout")
    try:
        # httpx timeouts apply per network phase. This outer bound includes
        # connect, headers and every body chunk in one remaining-time budget.
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(remaining, connect=min(5, remaining)),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST",
                    endpoint.rstrip("/") + "/" + path,
                    headers={"Authorization": "Bearer " + key},
                    json=payload,
                ) as response:
                    if not response.is_success and not classify_content_filter:
                        raise ModelCallError(
                            role + "_http_" + str(response.status_code)
                        )
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise ModelCallError(role + "_response_too_large")
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        if not response.is_success:
                            raise ModelCallError(role + '_http_' + str(response.status_code)) from None
                        raise
                    error = value.get('error') if isinstance(value,dict) else None
                    blocked = (classify_content_filter and isinstance(error,dict)
                        and (response.status_code in (200,400,403,422))
                        and is_content_filter(error))
                    if blocked:
                        failure = ModelCallError(role + '_content_filter')
                        failure.usage = value.get('usage')
                        raise failure
                    if not response.is_success:
                        raise ModelCallError(role + '_http_' + str(response.status_code))
                    if not isinstance(value, dict) or value.get("error"):
                        raise ModelCallError("invalid_" + role + "_response")
                    return value
    except ModelCallError:
        raise
    except (TimeoutError, httpx.TimeoutException):
        raise ModelCallError(role + "_timeout") from None
    except (httpx.HTTPError, ValueError, TypeError):
        raise ModelCallError("invalid_" + role + "_response") from None

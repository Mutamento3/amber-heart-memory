"""Shared public chat-completion transport for explicitly configured roles."""

import asyncio
from urllib.parse import urlsplit

from .http_transport import ModelCallError as CompletionModelError, post_json


def no_thinking_options(endpoint):
    """Use only the reasoning toggle documented for the configured host."""
    host = urlsplit(endpoint or "").hostname
    if host == "openrouter.ai":
        return {"reasoning": {"effort": "none"}}
    if host == "api.deepseek.com":
        return {"thinking": {"type": "disabled"}}
    if host in ("api.siliconflow.cn", "api.siliconflow.com"):
        return {"enable_thinking": False}
    return {}


def json_object_options(endpoint):
    """Ask hosts that document OpenAI-style JSON mode for a strict JSON object."""
    host = urlsplit(endpoint or "").hostname
    if host in ("openrouter.ai", "api.deepseek.com", "api.siliconflow.cn",
                "api.siliconflow.com", "api.openai.com"):
        return {"response_format": {"type": "json_object"}}
    return {}


class CompletionModel:
    attempts_per_model = 1

    def __init__(
        self,
        *,
        endpoint,
        key,
        model,
        backup=None,
        role,
        transport=None,
        timeout,
        request_options=None
    ):
        self.endpoint, self.key, self.role = endpoint, key, role
        self.models = list(dict.fromkeys(m for m in (model, backup) if m))
        self.transport, self.timeout = transport, timeout
        self.request_options = request_options or {}

    def validate_content(self, content):
        """Roles can reject unusable text before the next model attempt."""

    async def complete(self, prompt, material=None):
        if not self.endpoint or not self.key or not self.models:
            raise CompletionModelError(self.role + "_model_unconfigured")
        last_error = self.role + "_upstream_failed"
        last_exception = None
        # Extraction already renders the complete established prompt, including
        # its JSON material. Keep it intact rather than invent a second prompt.
        messages = (
            [dict(role="user", content=prompt)]
            if material is None
            else [
                dict(role="system", content=prompt),
                dict(role="user", content=material),
            ]
        )
        attempts = (model for model in self.models for _ in range(self.attempts_per_model))
        for model in attempts:
            value = None
            try:
                value = await post_json(
                    self.endpoint,
                    self.key,
                    "chat/completions",
                    dict(
                        self.request_options,
                        model=model,
                        stream=False,
                        messages=messages,
                    ),
                    role=self.role,
                    timeout=self.timeout,
                    transport=self.transport,
                )
                choices = value.get("choices")
                if not isinstance(choices, list) or len(choices) != 1:
                    raise CompletionModelError("invalid_" + self.role + "_response")
                choice = choices[0]
                content = choice.get("message", {}).get("content")
                if (
                    choice.get("finish_reason") != "stop"
                    or choice.get("message", {}).get("refusal")
                    or not isinstance(content, str)
                    or not content.strip()
                ):
                    raise CompletionModelError("incomplete_" + self.role + "_response")
                content = content.strip()
                self.validate_content(content)
                return dict(
                    content=content,
                    model=model,
                    usage=(
                        value.get("usage")
                        if isinstance(value.get("usage"), dict)
                        else None
                    ),
                )
            except CompletionModelError as exc:
                last_error = exc.code
                last_exception = exc
            except (ValueError, TypeError, AttributeError):
                last_error = "invalid_" + self.role + "_response"
                last_exception = CompletionModelError(last_error)
            # A truncated or malformed completion may still have consumed
            # tokens. Callers may account for metadata without keeping text.
            last_exception.model = model
            last_exception.usage = (
                value.get("usage") if isinstance(value, dict) else None
            )
        raise last_exception or CompletionModelError(last_error)

    def complete_sync(self, prompt, material=None):
        return asyncio.run(self.complete(prompt, material))

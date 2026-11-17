"""Pure protocol normalization and image-free per-turn context snapshots."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from . import chat_text as text


def timed_content(content, timestamp, settings):
    moment = datetime.fromisoformat(timestamp).astimezone(ZoneInfo(settings.timezone))
    prefix = text.MESSAGE_TIME.format(time=moment.strftime(text.MESSAGE_TIME_FORMAT))
    if isinstance(content, str):
        return prefix + content
    parts = deepcopy(content or [])
    if parts and parts[0]["type"] == "text":
        parts[0]["text"] = prefix + parts[0]["text"]
    else:
        parts.insert(0, {"type": "text", "text": prefix})
    return parts


def content_parts(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return []
    if not isinstance(content, list):
        raise ValueError("消息正文格式无效")
    parts = []
    for part in content:
        if not isinstance(part, dict):
            raise ValueError("消息正文格式无效")
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            if parts and parts[-1]["type"] == "text":
                parts[-1]["text"] += part["text"]
            else:
                parts.append({"type": "text", "text": part["text"]})
        elif part.get("type") in ("image_url", "image_placeholder"):
            if part["type"] == "image_url" and (
                not isinstance(part.get("image_url"), dict)
                or not isinstance(part["image_url"].get("url"), str)
                or not part["image_url"]["url"]
            ):
                raise ValueError("图片格式无效")
            parts.append({"type": "image_placeholder"})
        else:
            raise ValueError("首发仅支持文字和图片消息")
    return parts


def canonical_message(message):
    result = {"role": message["role"], "content": content_parts(message.get("content"))}
    if message.get("tool_calls"):
        result["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                },
            }
            for call in message["tool_calls"]
        ]
    if message["role"] == "tool":
        result["tool_call_id"] = message["tool_call_id"]
    return result


def freeze_message(message):
    result = canonical_message(message)
    result["content"] = result["content"] or None
    # No reasoning, signed thinking blocks, URLs or image data in Postgres.
    return result


def freeze_context(body, user_index, timestamp):
    return {
        "schema_version": 1,
        "frozen_at": timestamp,
        "prefix": [freeze_message(m) for m in body["messages"][:user_index]],
        "user": freeze_message(body["messages"][user_index]),
        "parameters": deepcopy(
            {k: v for k, v in body.items() if k not in ("messages", "stream", "stream_options")}
        ),
    }


def thaw_message(frozen, current=None):
    result = deepcopy(frozen)
    parts = result.get("content") or []
    if any(p["type"] == "image_placeholder" for p in parts):
        actual = current.get("content") if current else None
        images = (
            [p for p in actual if isinstance(p, dict) and p.get("type") == "image_url"]
            if isinstance(actual, list)
            else []
        )
        if len(images) != sum(p["type"] == "image_placeholder" for p in parts):
            raise ValueError("工具续轮需要前端重新携带本轮图片")
        image_index = 0
        for index, part in enumerate(parts):
            if part["type"] == "image_placeholder":
                parts[index] = deepcopy(images[image_index])
                image_index += 1
    # Keep ordinary text in the familiar string form accepted by every provider.
    if parts and all(p["type"] == "text" for p in parts):
        result["content"] = "".join(p["text"] for p in parts)
    return result


def tool_result_pairs(messages, pending):
    """Pair client results with original ledger IDs, without changing the wire.

    Operit rebuilds call IDs when constructing a request. When IDs do not match
    as a batch, fall back to call order with the same count and names.
    Results within either batch can arrive in a different order.
    """
    results = []
    for message in reversed(messages):
        if message["role"] != "tool":
            break
        results.insert(0, message)
    ids = [m["tool_call_id"] for m in results]
    original_ids = {call["id"] for call in pending}
    mismatch = "工具结果须完整对应本轮尚未完成的调用"
    if not pending or len(results) != len(pending) or len(ids) != len(set(ids)):
        raise ValueError(mismatch)
    if set(ids) == original_ids:
        return [(m["tool_call_id"], m) for m in results]
    previous = messages[-len(results) - 1] if len(messages) > len(results) else {}
    calls = previous.get("tool_calls", []) if previous.get("role") == "assistant" else []
    if [call["function"]["name"] for call in calls] != [
        call["function"]["name"] for call in pending
    ]:
        raise ValueError(mismatch)
    client_ids = [call["id"] for call in calls]
    if len(client_ids) != len(set(client_ids)) or set(client_ids) != set(ids):
        raise ValueError(mismatch)
    originals = {call["id"]: original["id"] for call, original in zip(calls, pending)}
    return [(originals[m["tool_call_id"]], m) for m in results]


def continuation_body(body, frozen, user_index, expected_tail, settings):
    """Replay frozen prefix, retaining frontend protocol-only fields this round.

    Full current-round messages come from the client, including reasoning and
    rebuilt call IDs. A minimal continuation uses the ledger for missing messages;
    images still have to be supplied by the frontend, never by a saved URL.
    """
    if body["model"] != frozen["parameters"]["model"]:
        raise ValueError("同一轮工具续接不能切换模型")
    messages = body["messages"]
    current_user = messages[user_index] if user_index is not None else None
    tail = messages[user_index + 1 :] if user_index is not None else messages
    if all(m["role"] == "tool" for m in tail):
        tail = [thaw_message(freeze_message(m)) for m in expected_tail[:-len(tail)]] + tail
    prefix = []
    actual_prefix = messages[:user_index] if user_index is not None else []
    for index, message in enumerate(frozen["prefix"]):
        actual = actual_prefix[index] if len(actual_prefix) == len(frozen["prefix"]) else None
        prefix.append(thaw_message(message, actual))
    user = thaw_message(frozen.get("current", frozen["user"]), current_user)
    if current_user is not None:
        # Preserve the original image requirement, but do not compare text or
        # protocol-only fields. The timestamp still belongs to the first request.
        current_images = sum(
            p["type"] == "image_placeholder" for p in content_parts(current_user.get("content"))
        )
        frozen_images = sum(
            p["type"] == "image_placeholder" for p in frozen["user"].get("content") or []
        )
        if current_images != frozen_images:
            raise ValueError("工具续轮需要前端重新携带本轮图片")
        user = deepcopy(current_user)
        if "current" in frozen:
            user["content"] = timed_content(user.get("content"), frozen["frozen_at"], settings)
    result = deepcopy(frozen["parameters"])
    result["messages"] = prefix + [user] + deepcopy(tail)
    result["stream"] = body.get("stream", False)
    if "stream_options" in body:
        result["stream_options"] = deepcopy(body["stream_options"])
    return result

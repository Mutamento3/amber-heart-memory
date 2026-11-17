"""Body-only material shared by summaries, diaries and card extraction."""

from datetime import date, datetime, timedelta

from .conversation_history import parts_text, render_turn
from .conversation_rounds import fingerprint
from .clock import business_day_bounds
from .space import read_policy


def segment_id(identifier):
    return "conversation-message:" + str(identifier)


def finished_day(connection, space, day, settings, now, purpose):
    policy = read_policy(connection, space)
    _, end = business_day_bounds(date.fromisoformat(day), settings)
    if now < end:
        return dict(status="retry", reason=purpose + "_day_not_finished")
    turns = connection.execute(
        """SELECT * FROM memory_core.conversation_turns
        WHERE bot_id=%s AND business_date=%s AND history_state='current' ORDER BY position_id""",
        (space, day),
    ).fetchall()
    if not turns:
        return dict(status="skip", reason="no_dialogue")
    retention = policy["conversation_retention_days"]
    cutoff = now - timedelta(days=retention) if retention is not None else None
    if any(
        t["content_deleted_at"] is not None
        or (cutoff and datetime.fromisoformat(t["created_at"]) <= cutoff)
        for t in turns
    ):
        return dict(status="skip", reason="source_expired")
    if any(t["generation_state"] in ("generating", "waiting_tools") for t in turns):
        return dict(status="retry", reason=purpose + "_turn_pending")
    return dict(status="ready", policy=policy, turns=turns)


def extraction_segments(connection, space, turns, settings):
    rows = raw_messages(connection, space, [t["id"] for t in turns])
    segments, days = [], {}
    for turn in turns:
        # render_turn drops blank assistant steps (a tool call with no text),
        # so pair segments only with the messages it actually renders.
        messages = [r for r in rows if r["turn_id"] == turn["id"] and (
            r["role"] == "user"
            or parts_text(r["content_parts"], r.get("image_descriptions")).strip())]
        rendered = render_turn(turn, messages, [], settings)
        if len(messages) != len(rendered):
            raise ValueError("抽卡正文分段与原消息不符")
        for message, body in zip(messages, rendered):
            sid = segment_id(message["id"])
            segments.append(
                dict(
                    segment_id=sid,
                    speaker=body["role"],
                    text=body["content"],
                    timestamp=message["occurred_at"],
                )
            )
            days[sid] = turn["business_date"]
    digest = fingerprint(
        dict(turns=[(t["id"], t["generation_state"]) for t in turns], segments=segments)
    )
    return segments, days, digest


def raw_messages(connection, space, ids):
    return (
        connection.execute(
            """SELECT * FROM memory_core.conversation_messages WHERE bot_id=%s
        AND turn_id=ANY(%s) AND content_deleted_at IS NULL ORDER BY turn_id,event_index""",
            (space, ids),
        ).fetchall()
        if ids
        else []
    )


def dialogue_input(connection, space, turns, policy, settings):
    rows = raw_messages(connection, space, [t["id"] for t in turns])
    dialogue, groups = [], []
    for turn in turns:
        messages = render_turn(
            turn, [r for r in rows if r["turn_id"] == turn["id"]], [], settings
        )
        groups.append(dict(turn_id=turn["id"], messages=messages))
        for message in messages:
            role = "user" if message["role"] == "user" else "assistant"
            dialogue.append(
                policy["participants"][role]["name"] + "：" + message["content"]
            )
    digest = fingerprint(
        dict(
            turns=[(t["id"], t["generation_state"]) for t in turns],
            messages=rows,
            policy_revision=policy["revision"],
        )
    )
    return "\n".join(dialogue), digest, groups

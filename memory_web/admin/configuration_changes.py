"""Describe differences from the draft's actual saved configuration snapshot."""


def changes(previous, current):
    if not previous:
        return [
            "这张旧草稿缺少生成时的设置记录，无法逐项还原差异；请核对当前的名字和又名。"
        ]
    result = []
    for role, label in (("user", "你的"), ("assistant", "TA 的")):
        before = previous.get("participants", {}).get(role, {})
        after = current.get("participants", {}).get(role, {})
        if before.get("name") != after.get("name"):
            result.append(
                f"{label}名字：{before.get('name','未设置')} → {after.get('name','未设置')}"
            )
        if before.get("aliases") != after.get("aliases"):
            result.append(f"{label}又名已调整")
    return result

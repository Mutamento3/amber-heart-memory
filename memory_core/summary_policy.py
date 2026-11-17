"""Summary thresholds and whole-round length protection, without storage or models."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class SummarySettings:
    trigger_messages: int = 28
    retain_turns: int = 2
    rollup_trigger: int = 8
    max_history_chars: int = 30000

    def __post_init__(self):
        for name in (
            "trigger_messages",
            "retain_turns",
            "rollup_trigger",
            "max_history_chars",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(
                    "摘要阈值、接力轮数、合并阈值和原话字符上限必须是正整数"
                )
        if self.trigger_messages % 2 or self.trigger_messages <= 2 * self.retain_turns:
            raise ValueError("每轮计两条，摘要触发条数须为偶数且大于接力条数")
        if self.rollup_trigger < 3:
            raise ValueError("二次摘要触发份数不能小于三")

    @classmethod
    def from_env(cls):
        names = {
            "trigger_messages": "MEMORY_SUMMARY_TRIGGER_MESSAGES",
            "retain_turns": "MEMORY_SUMMARY_RETAIN_TURNS",
            "rollup_trigger": "MEMORY_SUMMARY_ROLLUP_TRIGGER",
            "max_history_chars": "MEMORY_SUMMARY_MAX_HISTORY_CHARS",
        }
        try:
            return cls(
                **{
                    key: int(os.environ[env])
                    for key, env in names.items()
                    if env in os.environ
                }
            )
        except ValueError as exc:
            raise ValueError(
                "及时摘要配置无效，请核对条数、接力轮数和字符上限"
            ) from exc


def summary_batch(turns, settings, *, characters):
    """A delayed job consumes one fixed cycle; overflow stays with the relay."""
    available = len(turns) - settings.retain_turns
    if available <= 0:
        return []
    if 2 * len(turns) >= settings.trigger_messages:
        return turns[: settings.trigger_messages // 2 - settings.retain_turns]
    if characters > settings.max_history_chars:
        return turns[:available]
    return []


def protected_tail(groups, limit):
    """Keep a contiguous suffix of whole rounds within the character budget.

    A single oversized round is omitted as a whole rather than slicing its text.
    The real current user message is separate and is never cut by this helper.
    """
    kept, characters = [], 0
    for group in reversed(groups):
        size = sum(len(message["content"]) for message in group["messages"])
        if characters + size > limit:
            break
        kept.insert(0, group)
        characters += size
    omitted = groups[: len(groups) - len(kept)]
    return kept, omitted

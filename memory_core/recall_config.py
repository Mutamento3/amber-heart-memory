"""The sole defaults for public recall, matching the approved live behavior."""

from dataclasses import asdict, dataclass, field, fields
import os


@dataclass(frozen=True)
class RecallSettings:
    full_list_enabled: bool = field(default=True, metadata={"env": "MEMORY_RERANK_FULL_LIST_ENABLED"})
    tail_gate_enabled: bool = field(default=True, metadata={"env": "MEMORY_RERANK_TAIL_GATE_ENABLED"})
    exact_entity_gate_enabled: bool = field(default=False, metadata={"env": "MEMORY_RERANK_EXACT_ENTITY_GATE_ENABLED"})
    graph_entity_name_only_enabled: bool = field(default=False, metadata={"env": "MEMORY_GRAPH_ENTITY_NAME_ONLY_ENABLED"})
    evidence_enabled: bool = field(default=True, metadata={"env": "MEMORY_RERANK_EVIDENCE_ENABLED"})
    evidence_max_children: int = field(default=3, metadata={"env": "MEMORY_RERANK_EVIDENCE_MAX_CHILDREN"})
    evidence_max_title_chars: int = field(default=60, metadata={"env": "MEMORY_RERANK_EVIDENCE_MAX_TITLE_CHARS"})
    evidence_max_summary_chars: int = field(default=120, metadata={"env": "MEMORY_RERANK_EVIDENCE_MAX_SUMMARY_CHARS"})
    evidence_max_total_chars: int = field(default=480, metadata={"env": "MEMORY_RERANK_EVIDENCE_MAX_TOTAL_CHARS"})
    graph_enabled: bool = field(default=True, metadata={"env": "MEMORY_GRAPH_ENABLED"})

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.type is bool and type(value) is not bool:
                raise ValueError("召回开关必须是布尔值")
            if item.type is int and (type(value) is not int or value < 0):
                raise ValueError("命中依据上限必须是非负整数")

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        values = {}
        for item in fields(cls):
            raw = environ.get(item.metadata["env"])
            if raw is None:
                continue
            if item.type is bool:
                values[item.name] = raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                try:
                    values[item.name] = max(0, int(raw))
                except ValueError:
                    pass
        return cls(**values)

    def as_dict(self):
        return asdict(self)

    @classmethod
    def environment_defaults(cls):
        defaults = cls()
        return {item.metadata["env"]: getattr(defaults, item.name) for item in fields(cls)}

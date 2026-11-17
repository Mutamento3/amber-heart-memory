"""Synthetic-only response builder for the established two-step prompts."""

import json
import threading


class SyntheticExtractionHTTP:
    def __init__(self):
        self.failures = {}
        self.calls = []
        self.candidate_count = 1
        self.hook = None
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def respond(self, path, body):
        if path.endswith("/embeddings"):
            vector = [0, 1, 0] if "合成第二张" in body["input"] else [1, 0, 0]
            return 200, dict(data=[dict(index=0, embedding=vector)])
        prompt = body["messages"][0]["content"]
        context_json, rest = prompt.split("空间上下文 JSON：\n", 1)[1].split("\n\n", 1)
        context = json.loads(context_json)
        material = json.loads(rest.split("\n", 1)[1])
        stage = "selection" if "segments" in material else "structure"
        self.calls.append((stage, material))
        if self.hook:
            self.hook(stage, material)
        if self.block:
            self.started.set()
            self.release.wait(5)
        if self.failures.get(stage, 0):
            self.failures[stage] -= 1
            return 503, dict(
                error=dict(message="synthetic-secret-key MUST_NOT_PERSIST")
            )
        if stage == "selection":
            segment = next(s for s in material["segments"] if s["speaker"] == "user")
            if "合成" not in segment["text"]:
                raise ValueError("synthetic input required")
            memories = []
            for ordinal in range(self.candidate_count):
                title = "合成第一张" if ordinal == 0 else "合成第二张"
                content = (
                    context["participants"]["user"]["name"]
                    + "与"
                    + context["participants"]["assistant"]["name"]
                    + "讨论"
                    + title
                    + "计划。"
                )
                memories.append(
                    dict(
                        title=title,
                        content=content,
                        summary=content,
                        importance=8,
                        evidence=[
                            dict(
                                segment_id=segment["segment_id"],
                                quote=segment["text"][:80],
                            )
                        ],
                    )
                )
            result = dict(memories=memories, skipped_reason=None)
        else:
            user = context["participants"]["user"]
            result = dict(
                summary=material["summary"],
                experience_mode="shared",
                cognitive_status="stable",
                decay_layer="tier_4_episode",
                source_date=None,
                is_milestone=False,
                valence=None,
                arousal=None,
                topic_ids=[
                    next(t["id"] for t in context["topics"] if not t["is_fallback"])
                ],
                entities=[
                    dict(
                        ref="who",
                        name=user["name"],
                        known_entity_id=user["id"],
                        entity_type="PERSON",
                        entity_subtype=None,
                        aliases=[],
                        mention_role="subject",
                        description="合成人物",
                    ),
                    dict(
                        ref="plan",
                        name="合成计划",
                        known_entity_id=None,
                        entity_type="EVENT",
                        entity_subtype=None,
                        aliases=[],
                        mention_role="object",
                        description="合成计划描述",
                    ),
                ],
                relations=[
                    dict(
                        source_ref="who",
                        target_ref="plan",
                        relation_type="PARTICIPATES_IN",
                        relation_type_raw=None,
                        description="合成参与关系",
                        valid_from=None,
                    )
                ],
            )
        return 200, dict(
            choices=[
                dict(
                    message=dict(content=json.dumps(result, ensure_ascii=False)),
                    finish_reason="stop",
                )
            ],
            usage=dict(prompt_tokens=10, completion_tokens=3, total_tokens=13),
        )

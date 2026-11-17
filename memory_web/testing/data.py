"""Synthetic acceptance materials, explicitly registered fixed responses only."""

import json
from memory_core.storage.db import connect
from memory_core.cards.drafts import context_for, structure_input
from memory_core.cards.extraction_contract import render_prompt
from memory_core.cards.review import ReviewStore, entity_embedding_text
from memory_core.testing.fixed_models import FixedModels


def draft(
    dsn,
    space,
    key="one",
    title="周末的植物课程",
    content="小禾想在周末学习植物，林舟答应陪她一起做笔记。",
    summary="小禾与林舟计划周末一起学习植物。",
):
    store = ReviewStore(dsn, space)
    first = dict(
        title=title,
        content=content,
        summary=summary,
        importance=8,
        evidence=[dict(segment_id="s1", quote=content)],
    )
    segments = [
        dict(
            segment_id="s1",
            speaker="user",
            text=content,
            timestamp="2026-10-29T14:00:00+08:00",
        )
    ]
    extraction = store.submit("synthetic:" + key, "synthetic-request:" + key, segments)
    with connect(dsn) as connection:
        context = context_for(connection, space)
    user = context["participants"]["user"]
    topic_names = (
        ("兴趣爱好", "兴趣") if key == "processed" else ("工作与学习", "工作学习")
    )
    topic = next(t for t in context["topics"] if t["name"] in topic_names)
    structure = dict(
        summary=summary,
        experience_mode="shared",
        cognitive_status="stable",
        decay_layer="tier_4_episode",
        source_date="2026-10-29",
        is_milestone=False,
        valence=0.5,
        arousal=0.3,
        topic_ids=[topic["id"]],
        entities=[
            dict(
                ref="user",
                name=user["name"],
                known_entity_id=user["id"],
                entity_type="PERSON",
                entity_subtype=None,
                aliases=[],
                mention_role="subject",
                description="合成示例中的用户",
            ),
            dict(
                ref="course",
                name="植物课程",
                known_entity_id=None,
                entity_type="EVENT",
                entity_subtype=None,
                aliases=["园艺课程"],
                mention_role="object",
                description="一门合成的植物学习课程",
            ),
        ],
        relations=[
            dict(
                source_ref="user",
                target_ref="course",
                relation_type="PARTICIPATES_IN",
                relation_type_raw=None,
                description="用户计划参加植物课程",
                valid_from=None,
            )
        ],
    )
    responses = {
        "extract": {
            store.selection_prompt(extraction): json.dumps(
                dict(memories=[first], skipped_reason=None), ensure_ascii=False
            ),
            render_prompt(
                "structure",
                context,
                structure_input(first, "2026-10-29", segments, store.settings),
            ): json.dumps(structure, ensure_ascii=False),
        }
    }
    actual = store.extract(
        extraction, selector=FixedModels(responses), structurer=FixedModels(responses)
    )["drafts"][0]
    vector = [1.0] + [0.0] * (store.settings.dimensions - 1)
    embeddings = {
        summary: vector,
        entity_embedding_text(structure["entities"][1]): vector,
    }
    return actual, {"embed": embeddings}

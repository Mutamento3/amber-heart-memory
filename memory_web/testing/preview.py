"""Disposable synthetic browser acceptance server, only on the internal test network."""

import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from memory_core.storage.config import Settings
from memory_core.storage.db import connect
from memory_core.storage.install import install
from memory_core.storage.space import Participant, SpaceConfiguration
from memory_core.cards.review import ReviewStore
from memory_core.testing.fixed_models import FixedModels
from memory_core.conversations.conversations import ConversationStore
from memory_web.install import install_web
from memory_web.admin.password import write_password
from memory_web.testing.data import draft


def main():
    admin = os.environ["MEMORY_TEST_ADMIN_DSN"]
    database = os.environ.get("MEMORY_WEB_PREVIEW_DATABASE", "memory_web_preview")
    if (
        not database.startswith("memory_web_")
        or not database.replace("_", "").isalnum()
    ):
        raise ValueError("仅允许独立的 memory_web_ 合成预览库")
    resume = os.environ.get("MEMORY_WEB_PREVIEW_RESUME") == "1"
    if not resume:
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER memory_owner").format(
                    sql.Identifier(database)
                )
            )
        with psycopg.connect(
            make_conninfo(admin, dbname=database), autocommit=True
        ) as connection:
            connection.execute("CREATE EXTENSION vector WITH SCHEMA public")
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO memory_app").format(
                    sql.Identifier(database)
                )
            )
    owner = make_conninfo(os.environ["MEMORY_TEST_OWNER_DSN"], dbname=database)
    app = make_conninfo(os.environ["MEMORY_TEST_APP_DSN"], dbname=database)
    install(owner, Settings(3))
    install_web(owner)
    if resume:
        fixtures = json.loads(os.environ["MEMORY_WEB_PREVIEW_FIXTURES"])
        config = SpaceConfiguration(owner)
        # Fresh examples show the new templates without resetting reviewed spaces.
        for space, name, template in (
            ("template-lover", "林舟·新模板", "lover"),
            ("template-general", "远山·新模板", "general"),
        ):
            with connect(app) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM memory_core.space_policies WHERE bot_id=%s", (space,)
                ).fetchone()
            if exists:
                continue
            config.configure(
                space, Participant("小禾"), Participant(name), topic_template=template
            )
            if template == "lover":
                _, response = draft(app, space)
                fixtures["embed"].update(response["embed"])
                card, response = draft(
                    app,
                    space,
                    "processed",
                    title="留在窗边的绿意",
                    content="小禾把新买的绿植放在窗边，林舟帮她记下每周浇水一次。",
                    summary="小禾与林舟为窗边的绿植约定每周浇水。",
                )
                response["embed"] = {key: [0, 1, 0] for key in response["embed"]}
                ReviewStore(app, space).approve(
                    card["id"],
                    expected_version=card["version"],
                    expected_policy_revision=1,
                    request_key="preview-approved",
                    embedder=FixedModels(response),
                    embedding_model="explicit-fixed-synthetic-v1",
                )
                fixtures["embed"].update(response["embed"])
    else:
        config = SpaceConfiguration(owner)
        config.configure(
            "sample", Participant("小禾", ["禾禾"]), Participant("林舟", ["阿林"])
        )
        config.configure(
            "second", Participant("小禾"), Participant("远山"), topic_template="general"
        )
        pending, fixtures = draft(app, "sample")
        processed, other = draft(
            app,
            "sample",
            "processed",
            title="留在窗边的绿意",
            content="小禾把新买的绿植放在窗边，林舟帮她记下每周浇水一次。",
            summary="小禾与林舟为窗边的绿植约定每周浇水。",
        )
        other["embed"] = {key: [0, 1, 0] for key in other["embed"]}
        ReviewStore(app, "sample").approve(
            processed["id"],
            expected_version=processed["version"],
            expected_policy_revision=1,
            request_key="preview-approved",
            embedder=FixedModels(other),
            embedding_model="explicit-fixed-synthetic-v1",
        )
        fixtures["embed"].update(other["embed"])
        fixtures["embed"]["浏览器修改后的摘要"] = [1, 0, 0]
    if not resume:
        from memory_core.cards.cards import CardStore
        archive_store=ReviewStore(app,'sample')
        topic=archive_store.topics()[0]['id']
        for title,stamp in [
            ('去年的植物笔记','2026-10-15T12:00:00+08:00'),
            ('另一份合成植物笔记','2026-09-15T12:00:00+08:00'),
            ('合成花园观察记录','2026-10-20T12:00:00+08:00'),
        ]:
            archived=archive_store.write_card('合成归档卡片：小禾和林舟整理了去年的植物笔记。',[0,0,1],title=title,summary='归档的合成植物笔记。',source_date='2025-10-29')
            CardStore(app,'sample').edit(archived,dict(title=title,topic_ids=[topic],is_milestone=False))
            with connect(app) as connection:
                connection.execute("UPDATE memory_core.memory_items SET archive_reason='decay',archived_at=%s WHERE id=%s",(stamp,archived))
    password = Path("/tmp/password.hash")
    if not resume:
        store = ConversationStore(app, 'sample')
        from datetime import datetime, timedelta, timezone
        from memory_core.storage.clock import business_day_bounds, business_day_label
        from memory_core.daily.daily_tasks import DailyTasks
        from memory_core.daily.diaries import DiaryStore

        moment = datetime.now(timezone.utc)
        today = business_day_label(moment, store.settings)
        diary_store = DiaryStore(app, "sample")
        daily = DailyTasks(app, "sample")
        for offset in (2, 1):
            day = today - timedelta(days=offset)
            when = business_day_bounds(day, store.settings)[0] + timedelta(hours=1)
            body = {
                "model": "synthetic-preview",
                "messages": [{"role": "user", "content": "聊聊今天的小安排。"}],
            }
            record = store.begin(body, {"operation": "new"}, now=when)
            store.finish(
                record,
                content="我们一起记下了今天值得留下的小事。",
                finish_reason="stop",
                now=when,
            )
            task = daily.claim("diary", day.isoformat(), now=moment)
            diary_store.publish(
                diary_store.plan(day.isoformat(), now=moment),
                dict(
                    content="# 林舟的日记\n今天我们聊了窗边的绿植，也记下了明天的安排。\n\n有些平常的小事，回头读时仍让人觉得温暖。",
                    model="synthetic-preview-diary",
                ),
                task,
                now=moment,
            )
        with connect(app) as connection:
            card = connection.execute("SELECT id AS memory_id,title,content,source_date FROM memory_core.memory_items WHERE bot_id='sample' ORDER BY id LIMIT 1").fetchone()
        recall = {'status': 'ok', 'cards': [dict(card, number=1)]}
        examples = [
            ('窗边的绿植该浇水了吗？', '记得我们约定的是每周一次。今天可以先看看土壤。', 'complete', True),
            ('帮我写一句给自己的鼓励。', '慢慢来，每一小步', 'interrupted', None),
            ('下次想试什么新的安排？', '', 'upstream_failed', None),
            ('今天先聊聊新的想法。', '好呀，先从你最想聊的地方开始。', 'complete', False),
        ]
        for question, answer, state, used in examples:
            content=question
            if question=='窗边的绿植该浇水了吗？':
                from memory_core.testing.images import image_url
                content=[dict(type='text',text=question),dict(type='image_url',image_url=dict(url=image_url()))]
            body = {'model': 'synthetic-preview', 'messages': [{'role': 'user', 'content': content}]}
            record = store.begin(body, {'operation': 'new'})
            store.save_context(record, body, store.history_input(record), recall)
            store.finish(record, content=answer, state=state, finish_reason='stop' if state == 'complete' else None,
                reported=[1] if used is True else [], reported_none=used is False)
            if isinstance(content,list):
                from memory_core.conversations.message_images import MessageImages
                captions=MessageImages(app,'sample')
                source=captions.source(record)
                captions.publish(source['message_id'],[dict(description='合成的窗边绿植截图。',calls=[dict(
                    model='synthetic-preview-image',state='succeeded',usage=None,code=None)])])
        from memory_core.daily.summaries import SummaryStore
        from memory_core.daily.summary_policy import SummarySettings

        summaries = SummaryStore(
            app,
            "sample",
            SummarySettings(trigger_messages=4, retain_turns=1, rollup_trigger=3),
        )
        from memory_core.storage.clock import business_day_label
        from datetime import datetime, timezone

        day = business_day_label(datetime.now(timezone.utc), store.settings).isoformat()
        while plan := summaries.next_plan(day):
            summaries.save(
                plan,
                dict(
                    content=(
                        "合成二次摘要：他们讨论了绿植和日常安排。"
                        if plan["level"] == 2
                        else "合成一级摘要：保留这段日常对话。"
                    ),
                    model="synthetic-preview-summary",
                ),
            )
        body = {
            "model": "synthetic-preview",
            "messages": [{"role": "user", "content": "接着聊接下来的计划。"}],
        }
        record = store.begin(body, {"operation": "new"})
        store.save_context(
            record, body, store.history_input(record, max_history_chars=1), recall
        )
        store.finish(
            record,
            content="我们可以从最想完成的一件事开始。",
            finish_reason="stop",
            reported_none=True,
        )
        from memory_web.mcp.mcp_tools import render_result

        messages = [{"role": "user", "content": "帮我查查土豆和打雷。"}]
        body = {"model": "synthetic-preview", "messages": messages}
        record = store.begin(body, {"operation": "new"})
        store.save_context(record, body, store.history_input(record), {"status": "failed", "cards": []})
        result, _ = render_result("search", {
            "items": [], "total_count": 0, "ordering_stats": {}, "retrieval_stats": {},
        })
        for index, query in enumerate(("土豆", "打雷"), 1):
            call = {
                "id": f"synthetic-search-{index}",
                "type": "function",
                "function": {"name": "memory", "arguments": json.dumps({"action": "search", "query": query})},
            }
            preamble = " \n " if index == 1 else None
            store.finish(record, content=preamble, tools=[call], finish_reason="tool_calls")
            messages.extend([
                {"role": "assistant", "content": preamble, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": call["id"], "content": result},
            ])
            record = store.begin(body, {})
        store.finish(record, content="我换了两个词查，还是没找到。", finish_reason="stop")
    if not resume:
        from memory_web.testing.topic_summaries import seed
        seed(owner,app,fixtures)
    if not resume:
        from memory_core.bookmarks.recent_notes import RecentNotesStore
        from memory_core.bookmarks.bookmarks import BookmarkStore
        config.set_core_memory('sample', expected_revision=1,
            core_memory='小禾不喜欢浓咖啡，下午更愿意喝温热的茶。\n重要安排先一起商量，答应的事要放在心上。')
        RecentNotesStore(app, 'sample').save(dict(
            period='2026年11月3日 本次周期开始，比上次晚了两天。',
            health='★2026年11月5日 复查后继续按医嘱用药，两周后复诊。\n2026年11月5日 医生说两周后复诊，这段时间注意休息。',
            longterm='2026年10月18日 开始准备资格考试，每周末复习两章。\n2026年11月2日 报名了春季课程，月底前确认上课时间。',
            daily='2026年11月8日 把绿植搬到了窗边，约好周末一起换盆。'), 0)
        BookmarkStore(app, 'sample').execute(dict(section='daily', content='2026年11月9日 找到了喜欢的新书，周末想一起读。'))
    if resume:
        password.write_text(os.environ["MEMORY_WEB_PREVIEW_PASSWORD_HASH"])
        password.chmod(0o600)
    else:
        write_password(
            password,
            os.environ.get(
                "MEMORY_WEB_PREVIEW_PASSWORD", "synthetic acceptance password"
            ),
        )
    fixed = Path("/tmp/fixed.json")
    fixed.write_text(json.dumps(fixtures))
    # Explicit disposable acceptance configuration only, never a runtime default.
    from cryptography.fernet import Fernet
    os.environ.setdefault('MEMORY_ACCESS_KEY_MASTER_KEY', Fernet.generate_key().decode())
    os.environ.update(
        MEMORY_DSN=app,
        MEMORY_CONFIG_DSN=owner,
        MEMORY_WEB_ORIGIN=os.environ.get(
            "MEMORY_WEB_PREVIEW_ORIGIN", "http://localhost:8080"
        ),
        MEMORY_WEB_PASSWORD_FILE=str(password),
        MEMORY_WEB_LOCAL_HTTP="1",
        MEMORY_WEB_FIXED_MODELS_FILE=str(fixed),
    )
    import uvicorn

    uvicorn.run(
        "memory_web.testing.preview:preview_app",
        factory=True,
        host="0.0.0.0",
        port=8080,
        proxy_headers=False,
        access_log=False,
    )


def preview_app():
    from memory_web.app import create_app
    from memory_web.testing.recent_notes import SyntheticSecretary
    return create_app(recent_note_models=SyntheticSecretary())


if __name__ == "__main__":
    main()

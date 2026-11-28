"""Paginated, read-only admin views; all mutations stay in memory_core services."""

import re
from datetime import date, datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

from memory_core.storage.clock import business_day_label
from memory_core.storage.db import connect
from memory_core.conversations.conversation_history import parts_text
from memory_core.daily.summaries import summarized_turn_ids
from memory_core.storage.config import Settings as CoreSettings
from memory_core.cards.review_policy import ApprovalSettings, MANUAL_REVIEW
from memory_core.daily.daily_policy import DailySettings
from memory_core.storage.schema import (DELAYED_APPROVAL_VERSION, ARCHIVE_VERSION, TOPIC_SUMMARY_VERSION,
                                IMAGE_DESCRIPTION_VERSION, SUMMARY_VERSION, DIARY_VERSION,
                                RECENT_NOTES_VERSION, CORE_MEMORY_VERSION, IMPORT_VERSION, SUMMARY_PROGRESS_VERSION)
from memory_core.lifecycle.archive_policy import ArchiveSettings, receipt_health
from memory_core.cards.drafts import DraftStore, context_for
from memory_core.imports.imports import direct_import_draft_filter
from memory_core.cards.daily_cards import extraction_counts
from memory_core.recall.date_period import period_end, period_start
from memory_core.recall.query import keyword_query
from memory_core.recall.search_store import SearchStore
from memory_core.storage.space import read_policy, read_topics
from memory_core.recall.tool_history import ERROR, memory_items, object_value, one_line, result_text, tool_identity
from .configuration_changes import changes
from ..version import VERSION


def conversation_tool_line(call):
    name = one_line(call["tool_name"] or "未知工具", 80)
    label = f"调用工具「{name}」"
    result = call["result_payload"]
    outcome = "未返回" if result is None else "调用失败" if call["result_status"] == "failure" else "已返回"
    try:
        name, args = tool_identity(call["tool_name"], object_value(call["arguments"]))
        if name == "memory":
            action = args.get("action")
            actions = {
                "search": ("搜索记忆", "query"), "browse": ("浏览记忆", "text"),
                "get": ("读取记忆", "memory_id"), "get_sources": ("回查记忆来源", "memory_id"),
                "list_topics": ("查看记忆主题", None),
            }
            if isinstance(action, str) and action in actions:
                label, key = actions[action]
                target = args.get(key) if key else None
                if isinstance(target, str) and (target := one_line(target, 80)):
                    label += f"「{target}」"
                if outcome == "已返回" and action != "list_topics":
                    text = result_text({"payload": result})
                    if text.startswith("没有找到记忆 M-"):
                        outcome = "没找到"
                    elif ERROR.match(text):
                        outcome = "调用失败"
                    else:
                        items, _, _ = memory_items(action, text)
                        outcome = f"找到 {len(items)} 张" if items else "没找到"
        elif name == "mark_moment":
            label = "记书签"
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        # Opaque and older client results still have a tool name and a return status.
        pass
    return dict(event_index=call["event_index"], label=label, outcome=outcome)


DIARY_TITLE = re.compile(r"^(#\s.*|#?\s*\S{1,12}的日记[。.]?)$")


def diary_excerpt(head, limit=120):
    """Opening of a diary for the list: no title line, whitespace folded, about three lines."""
    lines = (head or "").split("\n")
    if len(lines) > 1 and DIARY_TITLE.match(lines[0].strip()) and "".join(lines[1:]).strip():
        lines.pop(0)
    text = " ".join(" ".join(lines).split())
    return text if len(text) <= limit else text[:limit] + "…"


class Catalog:
    def __init__(self, dsn):
        self.dsn = dsn
        self.approval_policy = ApprovalSettings.from_env()
        self.daily_policy = DailySettings.from_env()
        self.archive_policy = ArchiveSettings.from_env()

    def spaces(self):
        with connect(self.dsn) as connection:
            return connection.execute(
                """SELECT p.bot_id AS id,e.name FROM memory_core.space_policies p
                JOIN memory_core.entities e ON e.bot_id=p.bot_id AND e.id=p.assistant_entity_id ORDER BY p.bot_id"""
            ).fetchall()

    def configuration(self, space):
        with connect(self.dsn) as connection:
            policy = read_policy(connection, space)
            context = context_for(connection, space)
            topics = [{key: topic[key] for key in ("id", "name", "preset_key", "description", "is_fallback", "summary_eligible")}
                for topic in read_topics(connection, space, policy=policy)]
            # Only return fields that these pages manage.
            return {
                key: policy[key]
                for key in (
                    "revision",
                    "participants",
                    "topic_template",
                )
            } | {
                "acquaintance_date": policy.get("acquaintance_date"),
                "persona": policy.get("persona", ""),
                "core_memory": policy.get("core_memory", ""),
                "auto_approve_enabled": policy.get("auto_approve_enabled",policy.get("review_mode","auto")=="auto"),
                "auto_approve_delay_hours": self.approval_policy.delay_hours,
                "period_notes_enabled": policy.get("period_notes_enabled", True),
                "version": VERSION,
                "topics": topics,
                "known_entities": context["known_entities"],
                "review_snapshot": connection.execute(
                    "SELECT memory_core.configuration_snapshot(%s) AS value", (space,)
                ).fetchone()["value"],
            }

    def overview(self, space, *, today=None, extraction_configured=False, topic_summary_configured=False, now=None):
        """Read-only counts for the landing page; no model calls, no writes."""
        with connect(self.dsn) as connection:
            policy = read_policy(connection, space)
            installation=connection.execute('SELECT version,settings FROM memory_core.installation').fetchone()
            core=CoreSettings(**installation['settings'])
            zone=ZoneInfo(core.timezone)
            now = (now or datetime.now(zone)).astimezone(zone)
            today = today or now.date()
            month_start = datetime(today.year, today.month, 1, tzinfo=zone)
            ready_filter = direct_import_draft_filter(installation['version'],in_progress=True)
            counts = connection.execute(
                f"""SELECT
                (SELECT count(*) FROM memory_core.pending_memories p WHERE bot_id=%s
                 AND status IN ('pending','structure_pending','structure_failed'){ready_filter}) AS pending,
                (SELECT count(*) FROM memory_core.pending_memories WHERE bot_id=%s AND status='rejected') AS rejected,
                (SELECT count(*) FROM memory_core.memory_items WHERE bot_id=%s AND archive_reason IS NULL) AS cards,
                (SELECT count(*) FROM memory_core.memory_items WHERE bot_id=%s AND archive_reason IS NULL
                 AND created_at::timestamptz >= %s) AS cards_this_month""",
                (space, space, space, space, month_start),
            ).fetchone()
            topics = connection.execute(
                """SELECT t.id,t.name,t.is_fallback,count(m.id) AS cards
                FROM memory_core.topics t
                LEFT JOIN memory_core.memory_topics mt ON mt.bot_id=t.bot_id AND mt.topic_id=t.id
                LEFT JOIN memory_core.memory_items m ON m.bot_id=mt.bot_id AND m.id=mt.memory_id AND m.archive_reason IS NULL
                WHERE t.bot_id=%s GROUP BY t.id ORDER BY count(m.id) DESC,t.id""",
                (space,),
            ).fetchall()
            has_conversations = connection.execute("SELECT to_regclass('memory_core.conversation_turns') AS value").fetchone()['value']
            recall_counts = connection.execute("""SELECT
                count(*) FILTER (WHERE recall_snapshot->>'status'='timeout') AS timed_out,
                count(*) FILTER (WHERE recall_snapshot->>'status'='failed') AS failed,
                count(*) FILTER (WHERE recall_snapshot->>'status'='degraded') AS degraded
                FROM memory_core.conversation_turns WHERE bot_id=%s""", (space,)).fetchone() if has_conversations else dict(timed_out=0, failed=0, degraded=0)
            has_tasks = connection.execute(
                "SELECT to_regclass('memory_core.daily_tasks') AS value"
            ).fetchone()["value"]
            latest = (
                connection.execute(
                    "SELECT * FROM memory_core.daily_tasks WHERE bot_id=%s AND task_kind='extraction' ORDER BY business_date DESC LIMIT 1",
                    (space,),
                ).fetchone()
                if has_tasks
                else None
            )
            extraction = dict(
                configured=extraction_configured,
                status="waiting" if extraction_configured else "unconfigured",
                business_date=None,
                added_count=0,
                pending_count=0,
                calls=0,
                # Local clock time of the nightly batch, so the page can say when cards appear.
                run_time=self.daily_policy.run_time or f"{core.day_start_hour:02d}:15",
            )
            if latest:
                identifiers = (latest["result"] or {}).get("extraction_ids", [])
                extraction.update(
                    business_date=latest["business_date"],
                    last_error_code=latest["last_error_code"],
                    calls=len(latest.get("model_usage", [])),
                    **extraction_counts(connection, space, identifiers),
                )
                if extraction_configured:
                    extraction["status"] = latest["state"]
            auto_approval=(self.approval_policy.overview(connection,space,policy,core,self.daily_policy,now,
                include_summaries=installation['version']>=TOPIC_SUMMARY_VERSION,
                pending_filter=direct_import_draft_filter(installation['version']))
                if installation['version']>=DELAYED_APPROVAL_VERSION else dict(enabled=False,delay_hours=self.approval_policy.delay_hours,
                    ready_count=0,manual_count=0,next_run_at=None,overdue=False,last_error_code=None))
            summaries = dict(configured=topic_summary_configured,
                status='waiting' if topic_summary_configured else 'unconfigured')
            if installation['version'] >= TOPIC_SUMMARY_VERSION:
                counts['pending'] += connection.execute("SELECT count(*) AS n FROM memory_core.topic_summary_batches WHERE bot_id=%s AND status='pending'",(space,)).fetchone()['n']
                counts['rejected'] += connection.execute("SELECT count(*) AS n FROM memory_core.topic_summary_batches WHERE bot_id=%s AND status='rejected'",(space,)).fetchone()['n']
                latest_summary = connection.execute("SELECT state,business_date,last_error_code FROM memory_core.daily_tasks WHERE bot_id=%s AND task_kind='topic_summaries' ORDER BY business_date DESC LIMIT 1",(space,)).fetchone()
                if latest_summary and topic_summary_configured:
                    summaries.update(latest_summary,status=latest_summary['state'])
            archive_health = (receipt_health(connection, space, self.archive_policy, now)
                if installation['version'] >= ARCHIVE_VERSION else None)
            version = installation['version']
            status_fields = (
                (DIARY_VERSION, "(SELECT business_date FROM memory_core.conversation_diaries "
                 "WHERE bot_id=%s ORDER BY business_date DESC LIMIT 1) AS diary_date", (space,)),
                (SUMMARY_VERSION, "(SELECT business_date FROM memory_core.conversation_summaries "
                 "WHERE bot_id=%s AND content_deleted_at IS NULL ORDER BY id DESC LIMIT 1) AS summary_date", (space,)),
                (RECENT_NOTES_VERSION, "(SELECT count(*) FROM memory_core.moment_bookmarks "
                 "WHERE bot_id=%s AND business_date=%s) AS new_bookmarks", (space, business_day_label(now, core).isoformat())),
            )
            queries = [(query, params) for minimum, query, params in status_fields if version >= minimum]
            overview_entries = {}
            if queries:
                fields = ", ".join(query for query, _ in queries)
                params = tuple(value for _, values in queries for value in values)
                overview_entries.update(connection.execute("SELECT " + fields, params).fetchone())
            if version >= CORE_MEMORY_VERSION:
                overview_entries["core_chars"] = len(policy.get("core_memory") or "")
        created = datetime.fromisoformat(policy["created_at"]).astimezone(zone).date()
        met = policy.get("acquaintance_date")
        met_day = date.fromisoformat(met) if met else None
        return dict(
            user=policy["participants"]["user"]["name"],
            assistant=policy["participants"]["assistant"]["name"],
            today=today.isoformat(),
            acquaintance_date=met,
            # The acquaintance day itself counts as day 1; a future date has no count yet.
            days_together=(
                (today - met_day).days + 1 if met_day and met_day <= today else None
            ),
            created_date=created.isoformat(),
            days_collected=max(1, (today - created).days + 1),
            topics=topics,
            recall_counts=recall_counts,
            daily_extraction=extraction,
            auto_approval=auto_approval,
            archive_health=archive_health,
            topic_summaries=summaries,
            overview_entries=overview_entries,
            **counts,
        )

    def drafts(self, space, status, page, size):
        statuses = {
            "pending": ("pending", "structure_pending", "structure_failed"),
            "processed": ("approved", "rejected"),
            "rejected": ("rejected",),
        }[status]
        with connect(self.dsn) as connection:
            version = connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']
            base = """SELECT id,status,version,policy_revision,narrative->>'title' AS title,business_date,
                memory_item_id,duplicate_memory_id,'atomic'::text AS item_kind,created_at FROM memory_core.pending_memories p
                WHERE bot_id=%s AND status=ANY(%s)"""
            if status == 'pending':
                base += direct_import_draft_filter(version,in_progress=True)
            params = [space,list(statuses)]
            if version >= TOPIC_SUMMARY_VERSION:
                summary_statuses = ['pending'] if status=='pending' else ['rejected'] if status=='rejected' else ['approved','rejected','revoked']
                base += """ UNION ALL SELECT id,status,version,policy_revision,title,cycle_date AS business_date,
                    summary_card_id AS memory_item_id,NULL::integer AS duplicate_memory_id,'topic_summary'::text AS item_kind,created_at
                    FROM memory_core.topic_summary_batches WHERE bot_id=%s AND status=ANY(%s)"""
                params += [space,summary_statuses]
            total = connection.execute('SELECT count(*) AS n FROM ('+base+') review_queue',params).fetchone()['n']
            sql = 'SELECT * FROM ('+base+') review_queue ORDER BY created_at DESC,id DESC,item_kind'
            if size is not None:
                sql += ' LIMIT %s OFFSET %s'
                params += [size,(page-1)*size]
            rows = connection.execute(sql,params).fetchall()
        return dict(items=rows, total=total, page=page, size=size)

    def draft(self, space, identifier):
        store = DraftStore(self.dsn, space)
        draft = store.get(identifier)
        if draft["status"] == "deleted":
            raise ValueError("对应卡片已删除，审核记录只保留计数")
        configuration = self.configuration(space)
        return dict(
            draft=draft,
            source=store.source(identifier),
            configuration=configuration,
            manual_review=draft.get("error_code") == MANUAL_REVIEW,
            configuration_changes=(
                changes(draft.get("policy_snapshot"), configuration["review_snapshot"])
                if draft["policy_revision"] != configuration["revision"]
                else []
            ),
        )

    def archive_months(self, space):
        with connect(self.dsn) as connection:
            return connection.execute('''SELECT left(archived_at,7) AS month,count(*) AS cards
                FROM memory_core.memory_items WHERE bot_id=%s AND archive_reason IS NOT NULL
                GROUP BY left(archived_at,7) ORDER BY month DESC''', (space,)).fetchall()

    def cards(self, space, *, query, topic, kind, since, until, page, size, archive="active", archive_month=None):
        store = SearchStore(self.dsn, space)
        policy = store.policy()
        cleaned, terms = (
            keyword_query(query, policy["ignored_terms"]) if query.strip() else ("", [])
        )
        if query.strip() and not terms:
            return dict(items=[], total=0, page=page, size=size)
        parameters = []
        join = ""
        score = "NULL::double precision"
        if cleaned:
            # Apply filters and pagination AFTER the complete existing BM25 query,
            # so a matching card beyond the recall top-N remains browseable.
            join = "JOIN memory_core.search_keywords(%s,%s,2147483647,%s,%s) k ON k.memory_id=m.id"
            parameters += [cleaned, space, kind, archive != "active"]
            score = "k.raw_score"
        filters = ["m.bot_id=%s"]
        if archive != "all":
            filters.append("m.archive_reason IS "+("NULL" if archive=="active" else "NOT NULL"))
        parameters.append(space)
        if archive_month:
            if archive != 'archived':
                raise ValueError('归档月份只能用于已归档卡片')
            month = date.fromisoformat(archive_month + '-01')
            following = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
            filters.extend(['m.archived_at>=%s', 'm.archived_at<%s'])
            parameters.extend([month.isoformat(), following.isoformat()])
        if kind != "all":
            filters.append("m.item_kind=%s")
            parameters.append(kind)
        if topic is not None:
            filters.append(
                "EXISTS(SELECT 1 FROM memory_core.memory_topics mt WHERE mt.bot_id=m.bot_id AND mt.memory_id=m.id AND mt.topic_id=%s)"
            )
            parameters.append(topic)
        if since:
            filters.append(period_end("m.source_date") + ">=%s")
            parameters.append(since)
        if until:
            filters.append(period_start("m.source_date") + "<=%s")
            parameters.append(until)
        base = (
            " FROM memory_core.memory_items m "
            + join
            + " WHERE "
            + " AND ".join(filters)
        )
        with connect(self.dsn) as connection:
            total = connection.execute(
                "SELECT count(*) AS n" + base, parameters
            ).fetchone()["n"]
            rows = connection.execute(
                "SELECT m.id,m.title,m.summary,m.source_date,m.item_kind,m.archive_reason,m.archived_at,"
                + score
                + " AS keyword_score"
                + base
                + " ORDER BY keyword_score DESC NULLS LAST,m.id DESC LIMIT %s OFFSET %s",
                parameters + [size, (page - 1) * size],
            ).fetchall()
        return dict(items=rows, total=total, page=page, size=size)

    def card(self, space, identifier):
        store = SearchStore(self.dsn, space)
        with connect(self.dsn) as connection:
            card = connection.execute(
                """SELECT id,title,content,summary,item_kind,source_date,source_kind,experience_mode,
                cognitive_status,decay_layer,is_milestone,valence,arousal,primary_topic_id,archive_reason,archived_at FROM memory_core.memory_items
                WHERE bot_id=%s AND id=%s""",
                (space, identifier),
            ).fetchone()
            if card is None:
                raise ValueError("卡片不存在或属于其他空间")
            card["entities"] = connection.execute(
                """SELECT e.id,e.name,e.aliases,e.entity_type FROM memory_core.entities e
                JOIN memory_core.memory_entities me ON me.bot_id=e.bot_id AND me.entity_id=e.id WHERE me.bot_id=%s AND me.memory_id=%s ORDER BY e.id""",
                (space, identifier),
            ).fetchall()
            card["topics"] = connection.execute(
                """SELECT t.id,t.name FROM memory_core.topics t JOIN memory_core.memory_topics mt
                ON mt.bot_id=t.bot_id AND mt.topic_id=t.id WHERE mt.bot_id=%s AND mt.memory_id=%s ORDER BY t.id""",
                (space, identifier),
            ).fetchall()
            version = connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']
            card['topic_summary'] = (connection.execute('SELECT id,version FROM memory_core.topic_summary_batches WHERE bot_id=%s AND summary_card_id=%s',
                (space,identifier)).fetchone() if version>=TOPIC_SUMMARY_VERSION else None)
            if version>=IMPORT_VERSION and card['item_kind']=='atomic':
                card['import_original'] = connection.execute('''SELECT s.segments->0->>'text' AS text,s.deleted_at
                    FROM memory_core.pending_memories p JOIN memory_core.material_extractions e
                    ON (p.extraction_id,p.bot_id)=(e.id,e.bot_id) JOIN memory_core.source_materials s
                    ON (e.material_id,e.bot_id)=(s.id,s.bot_id)
                    WHERE p.bot_id=%s AND p.memory_item_id=%s AND s.material_kind='memory' ''',
                    (space,identifier)).fetchone()
        card["source"] = store.source_references([identifier]).get(identifier)
        return card

    def conversations(self, space, before, size):
        with connect(self.dsn) as c:
            if not c.execute(
                "SELECT to_regclass('memory_core.conversation_turns') AS value"
            ).fetchone()["value"]:
                return dict(items=[], total=0, has_more=False, next_before=None, size=size)
            # total keeps counting every current turn (the overview uses it to
            # tell whether a chat app ever connected); the list hides turns
            # whose content was already removed by retention cleanup.
            total = c.execute(
                "SELECT count(*) AS n FROM memory_core.conversation_turns WHERE bot_id=%s AND history_state='current'",
                (space,),
            ).fetchone()["n"]
            version = c.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
            summary_field = "summary_progress" if version >= SUMMARY_PROGRESS_VERSION else "NULL AS summary_progress"
            turns = c.execute(
                """SELECT id,position_id,business_date,created_at,generation_state,content_deleted_at,
                CASE WHEN jsonb_typeof(recall_snapshot->'cards')='array'
                     THEN jsonb_array_length(recall_snapshot->'cards') ELSE 0 END AS recall_count,
                context_manifest->'summary_ids' AS summary_ids,context_manifest->'history_omitted' AS history_omitted,
                """ + summary_field + """
                FROM memory_core.conversation_turns
                WHERE bot_id=%s AND history_state='current' AND content_deleted_at IS NULL
                AND (%s::bigint IS NULL OR position_id<%s) ORDER BY position_id DESC LIMIT %s""",
                (space, before, before, size + 1),
            ).fetchall()
            has_more = len(turns) > size
            turns = turns[:size]
            ids = [t["id"] for t in turns]
            summarized = set(summarized_turn_ids(c, space, ids=ids)) if ids and version >= SUMMARY_VERSION else set()
            image_field='image_descriptions' if version>=IMAGE_DESCRIPTION_VERSION else 'NULL::jsonb AS image_descriptions'
            messages = (
                c.execute(
                    "SELECT id,turn_id,request_id,event_index,role,content_parts,content_state,occurred_at,"+image_field+
                    " FROM memory_core.conversation_messages WHERE bot_id=%s AND turn_id=ANY(%s) ORDER BY event_index",
                    (space, ids),
                ).fetchall()
                if ids
                else []
            )
            requests = (
                c.execute(
                    """SELECT id,turn_id,state,usage,usage_receipt,error_stage,error_code FROM memory_core.conversation_requests
                WHERE bot_id=%s AND turn_id=ANY(%s) ORDER BY step_index""",
                    (space, ids),
                ).fetchall()
                if ids
                else []
            )
            tools = (
                c.execute(
                    """SELECT call.turn_id,call.event_index,call.tool_name,call.payload->'arguments' AS arguments,
                       result.payload AS result_payload,result.reported_status AS result_status
                       FROM memory_core.conversation_tool_events call
                       LEFT JOIN memory_core.conversation_tool_events result
                         ON (result.bot_id,result.turn_id,result.tool_call_id)=(call.bot_id,call.turn_id,call.tool_call_id)
                         AND result.kind='result' AND result.content_deleted_at IS NULL
                       WHERE call.bot_id=%s AND call.turn_id=ANY(%s)
                         AND call.kind='call' AND call.content_deleted_at IS NULL
                       ORDER BY call.event_index""",
                    (space, ids),
                ).fetchall()
                if ids
                else []
            )
        tools_by_turn = defaultdict(list)
        for tool in tools:
            tools_by_turn[tool["turn_id"]].append(conversation_tool_line(tool))
        for message in messages:
            message["display_text"]=parts_text(message["content_parts"],message.pop("image_descriptions"))
        for turn in turns:
            turn["summarized"] = turn["id"] in summarized
            turn["messages"] = [m for m in messages if m["turn_id"] == turn["id"]]
            turn["requests"] = [r for r in requests if r["turn_id"] == turn["id"]]
            turn["tool_calls"] = tools_by_turn[turn["id"]]
        next_before = turns[-1]["position_id"] if turns else None
        return dict(items=list(reversed(turns)), total=total, has_more=has_more, next_before=next_before, size=size)

    def conversation_recall(self, space, identifier):
        with connect(self.dsn) as c:
            turn = c.execute(
                """SELECT recall_snapshot,context_manifest FROM memory_core.conversation_turns
                WHERE bot_id=%s AND id=%s AND history_state='current'
                AND NOT (context_manifest ? 'deleted_fingerprints')""",
                (space, identifier),
            ).fetchone()
            if not turn:
                raise ValueError("轮次不存在、已被替换或属于其他空间")
            requests = c.execute(
                "SELECT id,usage_receipt FROM memory_core.conversation_requests WHERE bot_id=%s AND turn_id=%s ORDER BY step_index",
                (space, identifier),
            ).fetchall()
        return dict(**turn, requests=requests)

    def diaries(self, space, page, size):
        with connect(self.dsn) as c:
            if not c.execute(
                "SELECT to_regclass('memory_core.conversation_diaries') AS value"
            ).fetchone()["value"]:
                return dict(items=[], total=0, page=page, size=size)
            total = c.execute(
                "SELECT count(*) AS n FROM memory_core.conversation_diaries WHERE bot_id=%s",
                (space,),
            ).fetchone()["n"]
            items = c.execute(
                """SELECT id,business_date,revision,user_edited,updated_at,left(content,400) AS head
                FROM memory_core.conversation_diaries
                WHERE bot_id=%s ORDER BY business_date DESC LIMIT %s OFFSET %s""",
                (space, size, (page - 1) * size),
            ).fetchall()
        for item in items:
            item["excerpt"] = diary_excerpt(item.pop("head"))
        return dict(items=items, total=total, page=page, size=size)

    def diary(self, space, identifier):
        with connect(self.dsn) as c:
            row = c.execute(
                "SELECT * FROM memory_core.conversation_diaries WHERE bot_id=%s AND id=%s",
                (space, identifier),
            ).fetchone()
            if not row:
                raise ValueError("日记不存在或属于其他空间")
        return row

    def summaries(self, space, page, size):
        with connect(self.dsn) as c:
            if not c.execute(
                "SELECT to_regclass('memory_core.conversation_summaries') AS value"
            ).fetchone()["value"]:
                return dict(items=[], total=0, page=page, size=size)
            total = c.execute(
                "SELECT count(*) AS n FROM memory_core.conversation_summaries WHERE bot_id=%s AND merged_into_id IS NULL AND content_deleted_at IS NULL",
                (space,),
            ).fetchone()["n"]
            items = c.execute(
                """SELECT id,business_date,level,start_at,end_at,created_at,content_deleted_at,merged_into_id,
                CASE WHEN content_deleted_at IS NULL THEN content END AS content
                FROM memory_core.conversation_summaries WHERE bot_id=%s AND merged_into_id IS NULL
                AND content_deleted_at IS NULL ORDER BY id DESC LIMIT %s OFFSET %s""",
                (space, size, (page - 1) * size),
            ).fetchall()
        return dict(items=items, total=total, page=page, size=size)

    def summary(self, space, identifier):
        with connect(self.dsn) as c:
            summary = c.execute(
                "SELECT * FROM memory_core.conversation_summaries WHERE id=%s AND bot_id=%s",
                (identifier, space),
            ).fetchone()
            if not summary:
                raise ValueError("摘要不存在或属于其他空间")
            leaf_ids = (
                [summary["id"]]
                if summary["level"] == 1
                else [
                    summary[k]
                    for k in (
                        "first_summary_id",
                        "second_summary_id",
                        "third_summary_id",
                    )
                ]
            )
            version=c.execute('SELECT version FROM memory_core.installation').fetchone()['version']
            image_field=("CASE WHEN t.history_state='current' THEN m.image_descriptions ELSE NULL END"
                if version>=IMAGE_DESCRIPTION_VERSION else 'NULL::jsonb')
            turns = c.execute(
                """SELECT t.id,t.position_id,t.business_date,t.created_at,t.history_state,t.content_deleted_at,
                CASE WHEN t.history_state='current' THEN m.content_parts ELSE NULL END AS user_content,"""+
                image_field+" AS image_descriptions "+"""
                FROM memory_core.conversation_summary_turns st JOIN memory_core.conversation_turns t
                ON (t.id,t.bot_id)=(st.turn_id,st.bot_id) LEFT JOIN memory_core.conversation_messages m
                ON (m.turn_id,m.bot_id)=(t.id,t.bot_id) AND m.role='user'
                WHERE st.bot_id=%s AND st.summary_id=ANY(%s) ORDER BY t.position_id""",
                (space, leaf_ids),
            ).fetchall()
        for turn in turns:
            turn["user_display_text"]=parts_text(turn["user_content"],turn.pop("image_descriptions"))
        return dict(summary=summary, turns=turns)

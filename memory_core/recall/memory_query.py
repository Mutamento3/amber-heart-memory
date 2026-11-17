"""Space-bound access to approved cards; never acquires a conversation lock."""

from datetime import date, datetime, timezone
import re

from ..storage.clock import format_timestamp
from ..storage.db import connect
from .rerank import order_candidates
from .recall_config import RecallSettings
from .search import ThreeRouteSearch
from .search_store import SearchStore


MEMORY_ID = re.compile(r"^M-([1-9][0-9]*)$")
SEARCH_POOL = 60


def memory_number(value):
    match = MEMORY_ID.fullmatch(value or "")
    if not match or int(match[1]) > 2147483647:
        raise ValueError("memory_id 必须是有效的 M-数字 编号，例如 M-123")
    return int(match[1])


def resolve_dimension(value, rows, label):
    needle = value.casefold()
    exact, partial = [], []
    for row in rows:
        names = [row['name'], *(row.get('aliases') or [])]
        folded = [name.casefold() for name in names]
        if needle in folded:
            exact.append(row)
        elif any(needle in name or name in needle for name in folded):
            partial.append(row)
    matches = exact or partial
    if len(matches) != 1:
        reason = '没有找到' if not matches else '匹配到多项，请使用更准确的名称：'
        candidates = '、'.join(row['name'] for row in matches[:8])
        raise ValueError(f'{label}「{value}」{reason}{candidates}')
    return matches[0]['id']


class MemoryQueryStore(SearchStore):
    def cards(self, ids):
        if not ids:
            return {}
        with connect(self.dsn) as connection:
            rows = connection.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND id=ANY(%s)',
                                      (self.space_id, list(ids))).fetchall()
        return {row['id']: row for row in rows}

    def filters(self, args):
        since, until = args.get('date_from'), args.get('date_to')
        for value in (since, until):
            if value:
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    raise ValueError('日期必须是有效的 YYYY-MM-DD') from None
        if since and until and since > until:
            raise ValueError('date_from 不能晚于 date_to')
        scope = args.get('source_scope') or 'atomic'
        clauses, values = ['m.bot_id=%s'], [self.space_id]
        if scope != 'all':
            clauses.append('m.item_kind=%s')
            values.append(scope)
        if not args.get('include_outdated', False):
            clauses.append("m.cognitive_status<>'outdated'")
        if since:
            clauses.append('left(m.source_date,10)>=%s')
            values.append(since)
        if until:
            clauses.append('left(m.source_date,10)<=%s')
            values.append(until)
        if args.get('topic'):
            topic = resolve_dimension(args['topic'], self.topics(), '主题')
            clauses.append('''EXISTS (SELECT 1 FROM memory_core.memory_topics t
                WHERE t.bot_id=m.bot_id AND t.memory_id=m.id AND t.topic_id=%s)''')
            values.append(topic)
        if args.get('entity'):
            entity = resolve_dimension(args['entity'], self.entities(), '实体')
            clauses.append('''EXISTS (SELECT 1 FROM memory_core.memory_entities e
                WHERE e.bot_id=m.bot_id AND e.memory_id=m.id AND e.entity_id=%s)''')
            values.append(entity)
        if args.get('text'):
            # strpos treats %, _ and spaces literally, unlike LIKE or BM25.
            clauses.append("strpos(lower(concat_ws(E'\\n',m.title,m.summary,m.content)),lower(%s))>0")
            values.append(args['text'])
        return ' AND '.join(clauses), values

    def labels(self, connection, rows):
        if not rows:
            return rows
        links = connection.execute('''SELECT mt.memory_id,t.name,mt.topic_id FROM memory_core.memory_topics mt
            JOIN memory_core.topics t ON (t.id,t.bot_id)=(mt.topic_id,mt.bot_id)
            WHERE mt.bot_id=%s AND mt.memory_id=ANY(%s) ORDER BY t.id''',
            (self.space_id, [r['id'] for r in rows])).fetchall()
        for row in rows:
            topics = [link for link in links if link['memory_id'] == row['id']]
            topics.sort(key=lambda link: link['topic_id'] != row['primary_topic_id'])
            row['topics'] = [link['name'] for link in topics]
        return rows

    def list_topics(self, args):
        if not args.get('source_scope'):
            raise ValueError('action=list_topics 时 source_scope 必填：atomic / topic_summary / all')
        where, values = self.filters(args)
        with connect(self.dsn) as connection:
            rows = connection.execute('''SELECT DISTINCT t.name FROM memory_core.topics t
                JOIN memory_core.memory_topics mt ON (mt.topic_id,mt.bot_id)=(t.id,t.bot_id)
                JOIN memory_core.memory_items m ON (m.id,m.bot_id)=(mt.memory_id,mt.bot_id)
                WHERE ''' + where + ' ORDER BY t.name', values).fetchall()
        return dict(topics=[r['name'] for r in rows])

    def browse(self, args):
        where, values = self.filters(args)
        limit, cursor = args.get('limit') or 30, args.get('cursor', 0)
        direction = 'DESC' if args.get('order') == 'desc' else 'ASC'
        with connect(self.dsn) as connection:
            total = connection.execute('SELECT count(*) AS n FROM memory_core.memory_items m WHERE ' + where, values).fetchone()['n']
            rows = connection.execute('SELECT m.* FROM memory_core.memory_items m WHERE ' + where +
                f' ORDER BY m.source_date {direction} NULLS LAST,m.id {direction} LIMIT %s OFFSET %s',
                values + [limit, cursor]).fetchall()
            self.labels(connection, rows)
        return self.page(rows, total, cursor, limit)

    @staticmethod
    def page(rows, total, cursor, limit):
        return dict(items=rows, total_count=total, cursor=cursor,
                    next_cursor=cursor + len(rows) if rows and cursor + len(rows) < total else None)

    def get(self, identifier, *, now=None):
        timestamp = format_timestamp(now or datetime.now(timezone.utc), self.settings)
        with connect(self.dsn) as connection:
            # The row update serializes with archival/deletion. Usage and revival
            # commit together; no proxy round, receipt or review lock is involved.
            row = connection.execute('''UPDATE memory_core.memory_items SET
                used_count=used_count+1,last_used_at=%s,
                restored_at=CASE WHEN archive_reason IS NOT NULL THEN %s ELSE restored_at END,
                archive_reason=NULL,archived_at=NULL
                WHERE bot_id=%s AND id=%s RETURNING *''',
                (timestamp, timestamp, self.space_id, identifier)).fetchone()
            if row is None:
                raise ValueError(f'没有找到记忆 M-{identifier}，它可能已删除或不属于当前空间')
            self.labels(connection, [row])
        return dict(item=row)

    def get_sources(self, identifier, args):
        limit, cursor = args.get('limit') or 20, args.get('cursor', 0)
        with connect(self.dsn) as connection:
            parent = connection.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s AND id=%s',
                (self.space_id, identifier)).fetchone()
            if parent is None:
                raise ValueError(f'没有找到记忆 M-{identifier}')
            if parent['item_kind'] != 'topic_summary':
                raise ValueError('get_sources 需要主题总结的 M-编号')
            # Explicit historical links and current reverse links form one set.
            # Deleted cards are physically absent and cannot be rehydrated here.
            source_ids = parent['child_episode_ids'] or []
            rows = connection.execute('''SELECT * FROM memory_core.memory_items
                WHERE bot_id=%s AND (id=ANY(%s::integer[]) OR parent_reflection_id=%s)
                ORDER BY source_date ASC NULLS LAST,id''', (self.space_id, source_ids, identifier)).fetchall()
            total = len(rows)
            rows = rows[cursor:cursor + limit]
            self.labels(connection, rows)
        return dict(parent=parent, **self.page(rows, total, cursor, limit))

    def search(self, query, args, models, settings):
        # Manual lookup returns the matched cards themselves, including children.
        # Parent substitution belongs only to automatic proxy recall.
        scope = args.get('source_scope') or 'atomic'
        where, values = self.filters(args)
        stats = {}
        coarse = ThreeRouteSearch(self, settings, item_kind=scope, include_archived=True,
                                  candidate_limit=SEARCH_POOL).search(
            query, embedder=models, cleaner=models, diagnostics=stats)
        ids = [r['memory_id'] for r in coarse]
        with connect(self.dsn) as connection:
            eligible = connection.execute('SELECT m.* FROM memory_core.memory_items m WHERE ' + where +
                ' AND m.id=ANY(%s)', values + [ids]).fetchall()
            by_id = {r['id']: r for r in eligible}
        candidates = [{**hit, **by_id[hit['memory_id']]} for hit in coarse if hit['memory_id'] in by_id]
        ordered, ordering = order_candidates(query, candidates, models, full_list=True)
        self.check_revision(stats['policy_revision'])
        # No DB transaction spans a model call. Recheck after reranking so a
        # card deleted during that wait is not returned from an old snapshot.
        current = self.cards([row['memory_id'] for row in ordered])
        visible = []
        for hit in ordered:
            mid = hit['memory_id']
            card = current.get(mid)
            if card is None:
                continue
            if card['cognitive_status'] == 'outdated' and not args.get('include_outdated', False):
                continue
            visible.append({**hit, **card})
        with connect(self.dsn) as connection:
            self.labels(connection, visible)
        limit, cursor = args.get('limit') or 10, args.get('cursor', 0)
        return dict(**self.page(visible[cursor:cursor + limit], len(visible), cursor, limit),
                    retrieval_stats=stats, ordering_stats=ordering)


class MemoryQueryService:
    def __init__(self, store, models, settings=None):
        self.store, self.models = store, models
        self.settings = settings or RecallSettings.from_env()

    def execute(self, args):
        action = args['action']
        limit = args.get('limit')
        maximum = {'search': 20, 'get_sources': 30}.get(action, 50)
        if limit is not None and limit > maximum:
            raise ValueError(f'{action} 的 limit 最多为 {maximum}')
        if action == 'list_topics':
            return self.store.list_topics(args)
        if action == 'browse':
            return self.store.browse(args)
        if action == 'search':
            query = (args.get('query') or '').strip()
            if not query:
                raise ValueError('action=search 时 query 必填，只写一件要查的事')
            return self.store.search(query, args, self.models, self.settings)
        identifier = memory_number(args.get('memory_id'))
        if action == 'get':
            return self.store.get(identifier)
        if action == 'get_sources':
            return self.store.get_sources(identifier, args)
        raise ValueError('不支持的查记忆动作')

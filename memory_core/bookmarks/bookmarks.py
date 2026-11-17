"""Pending-only bookmark workbench, sharing a short write lock with publishing."""

from datetime import datetime, timezone
import re
from uuid import uuid4

from ..storage.clock import business_day_label
from ..storage.db import connect
from .recent_notes import RecentNotesStore
from .recent_note_text import SECTIONS, one_line

BOOKMARK_ID = re.compile(r'(?:[0-9a-f]{8}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', re.IGNORECASE)


def not_found(identifier):
    return f'没找到编号 {identifier} 的书签：可能已经整理、删除或撤回。如果有新进展，请用 add 新记一条。'


def bookmark(row):
    return dict(id=str(row['id'])[:8], full_id=str(row['id']), section=row['section'],
                section_label=SECTIONS[row['section']], content=row['content'], created_at=row['created_at'])


class BookmarkStore(RecentNotesStore):
    def list(self, section=None, *, limit=100):
        if section is not None and section not in SECTIONS:
            raise ValueError('section 必须是 period / health / longterm / daily')
        with connect(self.dsn) as c:
            rows = c.execute('''SELECT * FROM memory_core.moment_bookmarks WHERE bot_id=%s
                AND (%s::text IS NULL OR section=%s) ORDER BY created_at::timestamptz DESC,id DESC''',
                (self.space_id, section, section)).fetchall()
        rank = {value: index for index, value in enumerate(SECTIONS)}
        rows.sort(key=lambda row: rank[row['section']])
        items = rows if limit is None else rows[:limit]
        return dict(ok=True, count=len(items), total=len(rows), has_more=len(items)<len(rows),
                    items=[bookmark(row) for row in items])

    def execute(self, args, *, now=None):
        action = args.get('action') or 'add'
        section = args.get('section')
        if action not in ('add','list','update','delete'):
            raise ValueError('action 必须是 add / list / update / delete')
        if section is not None and section not in SECTIONS:
            raise ValueError('section 必须是 period / health / longterm / daily')
        if action=='list':
            return self.list(section)
        if action=='add' and section is None:
            raise ValueError('add 必须指定 section（period / health / longterm / daily）')
        content = one_line(args.get('content')) if action in ('add','update') else None
        identifiers = args.get('ids')
        if action in ('update','delete'):
            if not isinstance(identifiers,list) or not identifiers or (action=='update' and len(identifiers)!=1):
                raise ValueError('update 的 ids 必须恰好 1 个' if action=='update' else 'delete 的 ids 必须有 1 个或多个')
            if any(not isinstance(identifier,str) or not BOOKMARK_ID.fullmatch(identifier) for identifier in identifiers):
                raise ValueError('ids 只接受 8 位十六进制短编号或完整 UUID')
            identifiers = list(dict.fromkeys(identifier.lower() for identifier in identifiers))
        with connect(self.dsn) as c:
            self._lock(c)
            if action=='add':
                instant = now or datetime.now(timezone.utc)
                row = c.execute('''INSERT INTO memory_core.moment_bookmarks(id,bot_id,section,content,business_date,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (uuid4(), self.space_id, section, content, business_day_label(instant,self.settings).isoformat(), self.timestamp(instant))).fetchone()
                return dict(ok=True, **bookmark(row))
            changed, missing, ambiguous = [], [], []
            # Resolve every prefix against the original pending set. Deleting
            # one full UUID must not make an ambiguous sibling prefix unique.
            rows = c.execute('SELECT * FROM memory_core.moment_bookmarks WHERE bot_id=%s', (self.space_id,)).fetchall()
            for identifier in identifiers:
                matches = [row for row in rows if str(row['id']).startswith(identifier)]
                if not matches:
                    missing.append(identifier)
                    continue
                if len(matches)>1:
                    ambiguous.append(identifier)
                    continue
                before = matches[0]
                if action=='update':
                    row = c.execute('''UPDATE memory_core.moment_bookmarks SET content=%s,section=%s
                        WHERE bot_id=%s AND id=%s RETURNING *''',
                        (content, section or before['section'], self.space_id, before['id'])).fetchone()
                    if row:
                        return dict(ok=True, **bookmark(row), before={key:before[key] for key in ('section','content')})
                    missing.append(identifier)
                elif c.execute('DELETE FROM memory_core.moment_bookmarks WHERE bot_id=%s AND id=%s RETURNING id',
                        (self.space_id,before['id'])).fetchone():
                    changed.append(str(before['id'])[:8])
                else:
                    missing.append(identifier)
            errors = [not_found(identifier) for identifier in missing]
            errors += [f'编号 {identifier} 匹配到多条书签，未操作；请使用完整 UUID' for identifier in ambiguous]
            return dict(ok=bool(changed), deleted=changed, not_found=missing, ambiguous=ambiguous, errors=errors)

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import psycopg
from psycopg.types.json import Jsonb
from memory_core.conversations.chat_text import IMAGE_PLACEHOLDER
from memory_core.storage.config import Settings
from memory_core.conversations.conversations import ConversationStore
from memory_core.cards.daily_cards import DailyCardStore
from memory_core.daily.daily_tasks import DailyTasks
from memory_core.storage.db import connect
from memory_core.daily.diaries import DiaryStore
from memory_core.storage.install import install,MIGRATIONS
from memory_core.conversations.message_images import MessageImages
from memory_core.storage.space import SpaceConfiguration,Participant
from memory_core.daily.summaries import SummaryStore
from memory_core.daily.summary_policy import SummarySettings
from memory_core.testing.images import image_url
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_turns import call

NOW=datetime.fromisoformat('2026-11-05T12:00:00+08:00')


def result(description='合成的灰色方块。',*,blocked=False):
    return dict(description=description,calls=[dict(model='synthetic-vision',state='succeeded' if description else 'blocked' if blocked else 'failed',
        usage=dict(total_tokens=8),code=None if description else 'image_description_content_filter' if blocked else 'image_description_timeout')])


def body(count=2):
    parts=[dict(type='text',text='看一下这些图片。')]
    for index in range(count):
        parts += [dict(type='image_url',image_url=dict(url=image_url(100+index))),dict(type='text',text='图后的文字。')]
    return dict(model='synthetic-main',messages=[dict(role='user',content=parts)])


class ImageStoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp();install(self.owner,Settings(3))
        for space in ('one','two'):
            SpaceConfiguration(self.owner).configure(space,Participant('合成读者'),Participant('合成助手'))
        self.store=ConversationStore(self.app,'one');self.images=MessageImages(self.app,'one')
    def begin(self,count=2,finish=True):
        request=body(count);record=self.store.begin(request,{},now=NOW)
        if finish:self.store.finish(record,content='合成主模型回复',finish_reason='stop',now=NOW)
        source=self.images.source(record,now=NOW)
        return record,source,request
    def row(self,mid):
        with connect(self.app) as c:return c.execute('SELECT * FROM memory_core.conversation_messages WHERE id=%s',(mid,)).fetchone()
    def test_descriptions_render_in_order_without_changing_original_message_or_match(self):
        record,source,request=self.begin();before=self.row(source['message_id'])
        self.assertEqual(source['image_count'],2)
        self.assertTrue(self.images.publish(source['message_id'],[result('第一张：合成方块。'),result(None)],now=NOW))
        after=self.row(source['message_id'])
        self.assertEqual(after['content_parts'],before['content_parts']);self.assertEqual(after['match_fingerprint'],before['match_fingerprint'])
        regenerated=self.store.begin(request,dict(operation='regenerate',target_turn_id=record['turn_id']),now=NOW)
        with connect(self.app) as c:
            self.assertEqual(c.execute('SELECT resolved_operation FROM memory_core.conversation_requests WHERE id=%s',(regenerated['request_id'],)).fetchone()['resolved_operation'],'regenerate')
        self.store.finish(regenerated,state='upstream_failed',error_code='synthetic',now=NOW)
        current=self.store.begin(dict(model='synthetic-main',messages=[dict(role='user',content='接着聊。')]),dict(operation='new'),now=NOW)
        history=self.store.history_input(current)['history'][0]['content']
        self.assertIn('[图片：第一张：合成方块。]图后的文字。'+IMAGE_PLACEHOLDER,history)
        self.assertNotIn('data:image',str(after))
        self.store.finish(current,content='继续。',finish_reason='stop',now=NOW)
    def test_final_write_once_and_database_guard(self):
        _,source,_=self.begin(1);mid=source['message_id'];saved=[result()]
        self.assertTrue(self.images.publish(mid,saved,now=NOW))
        self.assertFalse(self.images.publish(mid,[result('不能覆盖。')],now=NOW))
        self.assertEqual(self.row(mid)['image_descriptions'],saved)
        for value in (None,[result('不能改。')]):
            with self.assertRaises(psycopg.errors.CheckViolation),connect(self.app) as c:
                c.execute('UPDATE memory_core.conversation_messages SET image_descriptions=%s WHERE id=%s',(Jsonb(value) if value is not None else None,mid))
    def test_concurrent_results_only_one_wins(self):
        _,source,_=self.begin(1);mid=source['message_id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            values=list(pool.map(lambda text:self.images.publish(mid,[result(text)],now=NOW),('结果甲。','结果乙。')))
        self.assertEqual(sorted(values),[False,True])
    def test_wrong_space_role_count_and_secrets_are_rejected(self):
        record,source,_=self.begin();mid=source['message_id']
        self.assertIsNone(MessageImages(self.app,'two').source(record,now=NOW))
        self.assertFalse(MessageImages(self.app,'two').publish(mid,[result()],now=NOW))
        for values in ([result()], [result('data:image/png;base64,SYNTHETIC'),result()],
                       [result('sk-'+('A'*30)),result()], [result()|dict(raw_url='should not store'),result()]):
            with self.assertRaises(ValueError):self.images.publish(mid,values,now=NOW)
        with connect(self.app) as c:
            assistant=c.execute("SELECT id FROM memory_core.conversation_messages WHERE turn_id=%s AND role='assistant'",(record['turn_id'],)).fetchone()['id']
        self.assertFalse(self.images.publish(assistant,[result()],now=NOW))
    def test_expiration_erases_captions_and_rejects_late_write(self):
        record,source,_=self.begin(1);mid=source['message_id']
        self.images.publish(mid,[result()],now=NOW)
        self.store.expire(now=NOW+timedelta(days=30))
        row=self.row(mid)
        self.assertIsNone(row['image_descriptions']);self.assertIsNone(row['content_parts']);self.assertIsNotNone(row['content_deleted_at'])
        self.assertFalse(self.images.publish(mid,[result('迟到。')],now=NOW+timedelta(days=30)))
    def test_expired_but_not_cleaned_source_is_not_captioned(self):
        record,source,_=self.begin(1)
        self.assertIsNone(self.images.source(record,now=NOW+timedelta(days=30)))
        self.assertFalse(self.images.publish(source['message_id'],[result()],now=NOW+timedelta(days=30)))
    def test_summary_diary_and_extraction_share_caption_text_and_stable_segment_ids(self):
        record,source,_=self.begin(1)
        second=self.store.begin(dict(model='synthetic-main',messages=[dict(role='user',content='下一句。')]),dict(operation='new'),now=NOW)
        self.store.finish(second,content='第二句回复。',finish_reason='stop',now=NOW)
        summaries=SummaryStore(self.app,'one',SummarySettings(trigger_messages=4,retain_turns=1))
        stale=summaries.next_plan('2026-11-05');self.assertIn(IMAGE_PLACEHOLDER,stale['material'])
        self.images.publish(source['message_id'],[result('合成的方块截图。')],now=NOW)
        self.assertIsNone(summaries.save(stale,dict(content='过期输入的摘要。',model='synthetic-summary'),now=NOW))
        plan=summaries.next_plan('2026-11-05');self.assertIn('[图片：合成的方块截图。]',plan['material']);self.assertNotIn('data:image',str(plan))
        later=NOW+timedelta(days=1)
        diary=DiaryStore(self.app,'one').plan('2026-11-05',now=later)
        self.assertIn('[图片：合成的方块截图。]',diary['material'])
        task=DailyTasks(self.app,'one').claim('extraction','2026-11-05',now=later)
        cards=DailyCardStore(self.app,'one').prepare(task,now=later)
        self.assertIn('[图片：合成的方块截图。]',str(cards['segments']))
        self.assertEqual(cards['segments'][0]['segment_id'],'conversation-message:'+str(source['message_id']))
    def test_tool_continuation_replays_frozen_prefix_after_old_caption_finishes(self):
        _,source,_=self.begin(1)
        request=dict(model='synthetic-main',messages=[dict(role='user',content='查一查。')])
        record=self.store.begin(request,dict(operation='new'),now=NOW)
        frozen=self.store.save_context(record,record['upstream_body'],self.store.history_input(record),dict(status='ok',cards=[]))
        before=deepcopy(frozen['messages'][:-1]);self.assertIn(IMAGE_PLACEHOLDER,str(before))
        self.store.finish(record,content='先查询。',tools=[call('synthetic-a')],finish_reason='tool_calls',now=NOW)
        self.images.publish(source['message_id'],[result('后来完成的图片描述。')],now=NOW)
        continuation=deepcopy(request)
        continuation['messages'] += [dict(role='assistant',content='先查询。',tool_calls=[call('synthetic-a')]),dict(role='tool',tool_call_id='synthetic-a',content='合成结果。')]
        step=self.store.begin(continuation,{},now=NOW)
        self.assertEqual(step['upstream_body']['messages'][:len(before)],before)
        self.assertIsNone(self.images.source(step,now=NOW))


class ImageMigrationTests(DatabaseTestCase):
    def test_upgrade_adds_one_nullable_column_and_rolls_back_on_failure(self):
        install(self.owner,Settings(3),target_version=18)
        SpaceConfiguration(self.owner).configure('one',Participant('合成读者'),Participant('合成助手'))
        store=ConversationStore(self.app,'one');record=store.begin(body(1),{},now=NOW)
        with connect(self.app) as c:
            before=c.execute('SELECT * FROM memory_core.conversation_messages').fetchall()
            columns=c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='memory_core' AND table_name='conversation_messages' ORDER BY ordinal_position").fetchall()
            tables=c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY tablename").fetchall()
        with tempfile.TemporaryDirectory() as folder:
            shutil.copytree(MIGRATIONS,Path(folder)/'migrations')
            file=Path(folder)/'migrations/019_image_descriptions.sql';file.write_text(file.read_text()+'\nSELECT 1/0;')
            with patch('memory_core.storage.install.MIGRATIONS',file.parent),self.assertRaises(psycopg.errors.DivisionByZero):install(self.owner,Settings(3))
        with connect(self.app) as c:self.assertEqual(c.execute('SELECT version FROM memory_core.installation').fetchone()['version'],18)
        self.assertTrue(install(self.owner,Settings(3),target_version=19));self.assertFalse(install(self.owner,Settings(3),target_version=19))
        with connect(self.app) as c:
            after=c.execute('SELECT * FROM memory_core.conversation_messages').fetchall()
            self.assertIsNone(after[0].pop('image_descriptions'));self.assertEqual(after,before)
            updated=c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='memory_core' AND table_name='conversation_messages' ORDER BY ordinal_position").fetchall()
            self.assertEqual(updated,columns+[dict(column_name='image_descriptions')])
            self.assertEqual(c.execute("SELECT tablename FROM pg_tables WHERE schemaname='memory_core' ORDER BY tablename").fetchall(),tables)

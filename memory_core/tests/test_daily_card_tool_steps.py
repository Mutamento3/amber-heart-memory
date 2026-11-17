from copy import deepcopy
from datetime import datetime,timedelta

from memory_core.config import Settings
from memory_core.conversations import ConversationStore
from memory_core.daily_cards import DailyCardStore
from memory_core.daily_tasks import DailyTasks
from memory_core.db import connect
from memory_core.install import install
from memory_core.space import SpaceConfiguration,Participant
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_turns import call

NOW=datetime.fromisoformat('2026-11-05T12:00:00+08:00')


class BlankToolStepTests(DatabaseTestCase):
    def setUp(self):
        super().setUp();install(self.owner,Settings(3))
        SpaceConfiguration(self.owner).configure('one',Participant('合成读者'),Participant('合成助手'))
        self.store=ConversationStore(self.app,'one')

    def test_blank_tool_step_does_not_block_daily_extraction(self):
        request=dict(model='synthetic-main',messages=[dict(role='user',content='帮我查一下。')])
        record=self.store.begin(request,dict(operation='new'),now=NOW)
        self.store.finish(record,content='\n\n\n\n',tools=[call('synthetic-a')],finish_reason='tool_calls',now=NOW)
        continuation=deepcopy(request)
        continuation['messages'] += [dict(role='assistant',content='\n\n\n\n',tool_calls=[call('synthetic-a')]),
            dict(role='tool',tool_call_id='synthetic-a',content='合成结果。')]
        step=self.store.begin(continuation,{},now=NOW)
        self.store.finish(step,content='查到了。',finish_reason='stop',now=NOW)
        with connect(self.app) as c:
            roles=[r['role'] for r in c.execute('SELECT role FROM memory_core.conversation_messages ORDER BY turn_id,event_index').fetchall()]
        self.assertEqual(roles.count('assistant'),2)
        later=NOW+timedelta(days=1)
        task=DailyTasks(self.app,'one').claim('extraction','2026-11-05',now=later)
        cards=DailyCardStore(self.app,'one').prepare(task,now=later)
        self.assertEqual(cards['status'],'ready')
        self.assertEqual([s['speaker'] for s in cards['segments']],['user','assistant'])
        self.assertIn('查到了。',cards['segments'][1]['text'])

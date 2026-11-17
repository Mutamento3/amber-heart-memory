import asyncio
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from cryptography.fernet import Fernet

from memory_core.config import Settings
from memory_core.db import connect
from memory_core.install import install
from memory_core.postgres import PostgresStore
from memory_core.space import Participant, SpaceConfiguration
from memory_core.tests.support import DatabaseTestCase
from memory_core.tests.test_receipt_judge import DAY, seed_round
from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.install import install_web
from memory_web.password import write_password
from memory_web.receipt_judge_model import ReceiptJudgeModel

ORIGIN = 'https://memory.example.test'
RUN = datetime.fromisoformat('2026-11-06T04:00:00+08:00')


class ReceiptJudgeModelTests(unittest.TestCase):
    def test_judge_sends_only_the_configured_hosts_thinking_option(self):
        for host, expected in (
            ("api.siliconflow.cn", {"enable_thinking": False}),
            ("api.siliconflow.com", {"enable_thinking": False}),
            ("openrouter.ai", {"reasoning": {"effort": "none"}}),
            ("api.deepseek.com", {"thinking": {"type": "disabled"}}),
            ("compatible.example.test", {}),
        ):
            with self.subTest(host=host):
                settings = WebSettings(
                    "test", "owner", ORIGIN, Path("/tmp/synthetic.hash"),
                    upstream_base_url=f"https://{host}/v1",
                    upstream_api_key="synthetic-key",
                    access_key_master=Fernet.generate_key().decode(),
                    receipt_judge_model="synthetic-judge",
                )

                def reply(request):
                    self.assertEqual(request.url.path, "/v1/chat/completions")
                    self.assertEqual(
                        json.loads(request.content),
                        dict(
                            model="synthetic-judge",
                            stream=False,
                            messages=[
                                dict(role="system", content="合成规则"),
                                dict(role="user", content="合成材料"),
                            ],
                            max_tokens=2000,
                            temperature=0,
                            **expected,
                        ),
                    )
                    return httpx.Response(
                        200,
                        json={"choices": [{
                            "message": {"content": '{"results":[]}'},
                            "finish_reason": "stop",
                        }]},
                    )

                records = []
                model = ReceiptJudgeModel(settings, transport=httpx.MockTransport(reply))
                self.assertEqual(
                    model.judge("合成规则", "合成材料", records.append)["content"],
                    '{"results":[]}',
                )
                self.assertEqual(records[0]["state"], "succeeded")


class StubJudge:
    def __init__(self, configured=True, fail=False):
        self.configured, self.fail, self.calls = configured, fail, []

    def judge(self, prompt, material, record):
        self.calls.append(material)
        if self.fail:
            record(dict(stage='receipt_judge', model='stub-judge', state='failed', usage=None, code='synthetic_failure'))
            raise RuntimeError('synthetic failure')
        record(dict(stage='receipt_judge', model='stub-judge', state='succeeded', usage=None))
        turns = [int(line.split()[-1]) for line in material.splitlines() if line.startswith('### 轮次 ')]
        return dict(content=json.dumps(dict(results=[dict(turn=t, used=[1]) for t in turns])), model='stub-judge')


class ReceiptJudgeDailyTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(super().tearDown)
        install(self.owner, Settings(3))
        install_web(self.owner)
        SpaceConfiguration(self.owner).configure('one', Participant('Synthetic Reader'), Participant('Synthetic Assistant'))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        password = Path(folder.name) / 'password.hash'
        write_password(password, 'synthetic password')
        self.settings = WebSettings(self.app, self.owner, ORIGIN, password)
        self.card = PostgresStore(self.app, 'one').write_card('合成正文', [1, 0, 0], title='合成标题', source_date='2026-11-01')

    def tearDown(self):
        pass

    def run_daily(self, judge):
        app = create_app(self.settings, daily_clock=lambda: RUN, receipt_judge_models=judge)
        asyncio.run(app.state.daily.run_space('one', RUN))

    def task(self):
        with connect(self.app) as c:
            return c.execute("""SELECT state,result,model_usage FROM memory_core.daily_tasks
                WHERE bot_id='one' AND task_kind='receipts' AND business_date=%s""", (DAY,)).fetchone()

    def receipt(self, request):
        with connect(self.app) as c:
            return c.execute('SELECT usage_receipt FROM memory_core.conversation_requests WHERE id=%s', (request,)).fetchone()['usage_receipt']

    def test_configured_judge_runs_for_the_finished_day_once(self):
        _, request = seed_round(self.app, [self.card], '合成回复')
        judge = StubJudge()
        self.run_daily(judge)
        task = self.task()
        self.assertEqual(task['state'], 'done')
        self.assertEqual((task['result']['judged'], task['result']['candidates']), (1, 1))
        self.assertEqual(task['model_usage'][0]['stage'], 'receipt_judge')
        self.assertEqual(self.receipt(request)['status'], 'judged')
        self.run_daily(judge)
        self.assertEqual(len(judge.calls), 1)

    def test_unconfigured_judge_creates_no_task(self):
        _, request = seed_round(self.app, [self.card], '合成回复')
        self.run_daily(StubJudge(configured=False))
        self.assertIsNone(self.task())
        self.assertEqual(self.receipt(request)['status'], 'unreported')

    def test_failed_judge_marks_task_failed_and_keeps_receipts(self):
        _, request = seed_round(self.app, [self.card], '合成回复')
        self.run_daily(StubJudge(fail=True))
        self.assertEqual(self.task()['state'], 'failed')
        self.assertEqual(self.receipt(request)['status'], 'unreported')

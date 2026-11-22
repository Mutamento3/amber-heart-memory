"""Run with python -m memory_web.tests.browser_import_zip after installing Playwright Chromium."""

from io import BytesIO
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import parse_qs, urlsplit
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from playwright.sync_api import expect, sync_playwright


ORIGIN = "http://import.test"
STATIC = Path(__file__).resolve().parents[1] / "static"
PAGE = """<!doctype html><main></main><script type="module">
import {importsPage} from "/static/imports.js";
const api = async (path, {method, file, body} = {}) => {
  const response = await fetch(path, {method, body: file ?? JSON.stringify(body)});
  return response.json();
};
await importsPage(document.querySelector("main"), {api, read: api,
  keepView() {}, url: path => path, esc: String, state: {epoch: 1}, epoch: 1});
</script>"""


class NonSeekable(BytesIO):
    def seek(self, *args):
        raise OSError("streaming archive")


def archive_bytes(entries):
    buffer = NonSeekable()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.comment = b"Synthetic comment"
        for name, content in entries:
            archive.writestr(name, content)
    return buffer.getvalue()


GEMINI = (
    ("Takeout/我的活動/聊天記錄/MyActivity.json",
     b'[{"title":"synthetic","header":"Gemini Apps","body":"' + Random(7).randbytes(8192).hex().encode() + b'"}]'),
    ("Takeout/我的活動/搜尋/MyActivity.json", b'[{"metadata":{"header":"Gemini Apps"},"header":"Search"}]'),
)


class BrowserImportZipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch()
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.jobs, self.uploads, self.actions = [], [], []
        self.upload_filenames = []
        self.page = self.browser.new_page(viewport={"width": 375, "height": 812})
        self.page.add_init_script("try{localStorage.setItem('amber-guide-seen','1')}catch(e){}")
        self.addCleanup(self.page.close)
        self.page.route(ORIGIN + "/**", self.respond)
        self.page.add_init_script("""
          window.bytesRead = 0;
          const read = Blob.prototype.arrayBuffer;
          Blob.prototype.arrayBuffer = function () {
            window.bytesRead += this.size;
            return read.call(this);
          };
        """)

    def respond(self, route):
        url = urlsplit(route.request.url)
        if url.path == "/":
            return route.fulfill(body=PAGE, content_type="text/html")
        if url.path.startswith("/static/"):
            return route.fulfill(path=STATIC / Path(url.path).name, content_type="text/javascript")
        if url.path == "/imports":
            return route.fulfill(json=dict(jobs=self.jobs, configured=True, lifecycle_supported=True,
                max_upload_bytes=90 * 1024 ** 2, max_json_bytes=1024 ** 3))
        if url.path == "/imports/upload":
            query = parse_qs(url.query)
            self.uploads.append((query["source"][0], route.request.post_data_buffer))
            self.upload_filenames.append(query["filename"][0])
            return route.fulfill(json={"id": "synthetic"})
        if url.path.endswith("/preview"):
            identifier = url.path.split("/")[-2]
            source = self.uploads[-1][0] if identifier == "synthetic" else next(
                job["source"] for job in self.jobs if job["id"] == identifier)
            return route.fulfill(json=dict(source=source, conversations=[], assistant_groups=[],
                memory_groups=[], model_calls_min=0, model_calls_max=0, material_count=0, skipped_count=0))
        if url.path.endswith(("/discard", "/end")):
            identifier, action = url.path.split("/")[-2:]
            self.actions.append((identifier, action))
            job = next(job for job in self.jobs if job["id"] == identifier)
            if action == "discard":
                self.jobs.remove(job)
            else:
                job.update(status="completed", error_code="import_ended")
                job["progress"].update(failed=0, running=0)
            return route.fulfill(json={})
        route.abort()

    def upload(self, source, content, *, name="export.zip"):
        count = len(self.uploads)
        self.page.locator("#import-source").select_option(source)
        file = dict(name=name, mimeType="application/octet-stream", buffer=content) if isinstance(content, bytes) else content
        self.page.locator("#import-file").set_input_files(file)
        with self.page.expect_response(lambda response: "/imports/synthetic/preview" in response.url):
            self.page.get_by_role("button", name="上传并预览", exact=True).click()
        self.assertEqual(len(self.uploads), count + 1)
        sent_source, body = self.uploads[-1]
        self.assertEqual(sent_source, source)
        return body

    def test_only_needed_zip_members_are_sent_for_each_source(self):
        self.page.goto(ORIGIN)
        sources = {
            "chatgpt": [("conversations.json", b"[]"), ("folder/conversations-002.json", b"[]")],
            "claude": [("folder/conversations.json", b"[]")],
            "gemini": [GEMINI[0]],
            "deepseek": [("manifest.json", b"{}"), ("chat_session_index.json", b"[]"), ("chat_sessions/a.json", b"{}")],
            "kelivo": [("database/kelivo.db", b"synthetic-db"), ("settings.json", b"{}")],
        }
        extras = [
            ("photos/photo.png", b"synthetic-image"), ("chat_sessions/a_files/photo.png", b"synthetic-image"),
            ("user.json", b"account"), ("manifest.json", b"unrelated"), ("database/other.db", b"other-db"),
        ]
        for source, expected in sources.items():
            with self.subTest(source=source):
                ignored = [entry for entry in extras if entry[0] not in dict(expected)]
                if source == "gemini":
                    ignored.append(GEMINI[1])
                payload = self.upload(source, archive_bytes(expected + ignored))
                with ZipFile(BytesIO(payload)) as archive:
                    self.assertEqual(archive.namelist(), list(dict(expected)))
                    self.assertEqual({entry: archive.read(entry) for entry in archive.namelist()}, dict(expected))
        raw = b'[{"synthetic":true}]'
        self.assertEqual(self.upload("chatgpt", raw, name="conversations.json"), raw)

    def test_operit_uploads_single_json_or_two_files_and_filters_existing_zip(self):
        self.page.goto(ORIGIN)
        chats = b'{"archiveType":"operit_chat_archive","formatVersion":2,"chats":[]}'
        memories = b'{"memories":[],"links":[]}'
        self.assertEqual(self.upload("operit", chats, name="arbitrary-chat-name.json"), chats)
        self.assertEqual(self.upload_filenames[-1], "arbitrary-chat-name.json")
        paired = self.upload("operit", [
            dict(name="合成聊天.json", mimeType="application/json", buffer=chats),
            dict(name="合成记忆.json", mimeType="application/json", buffer=memories),
        ])
        self.assertEqual(self.upload_filenames[-1], "operit.zip")
        with ZipFile(BytesIO(paired)) as archive:
            self.assertEqual([archive.read(name) for name in archive.namelist()], [chats, memories])
        entries = [("folder/chats.JSON", chats), ("library.json", memories)]
        uploaded = self.upload("operit", archive_bytes(entries + [
            ("files/photo.png", b"synthetic attachment"), ("__MACOSX/._library.json", b"metadata"),
        ]))
        with ZipFile(BytesIO(uploaded)) as archive:
            self.assertEqual({name: archive.read(name) for name in archive.namelist()}, dict(entries))

    def test_fallback_sends_original_zip(self):
        self.page.goto(ORIGIN)
        source = archive_bytes([("conversations.json", b"[]")])
        encrypted = bytearray(source)
        encrypted[encrypted.index(b"PK\x01\x02") + 8] |= 1
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive, archive.open("conversations.json", "w", force_zip64=True) as entry:
            entry.write(b"[]")
        for payload in (bytes(encrypted), buffer.getvalue(), b"unreadable ZIP"):
            with self.subTest(size=len(payload)):
                self.assertEqual(self.upload("chatgpt", payload), payload)
        self.page.evaluate("window.DecompressionStream = undefined")
        ambiguous = archive_bytes(GEMINI)
        self.assertEqual(self.upload("gemini", ambiguous), ambiguous)

    def test_large_disk_zip_reads_only_small_slices(self):
        self.page.goto(ORIGIN)
        client = self.page.context.new_cdp_session(self.page)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "large.zip"
            with ZipFile(path, "w", ZIP_STORED) as archive:
                archive.writestr("conversations.json", b"[]")
                with archive.open("photos/big-image.bin", "w") as asset:
                    block = bytes(1024 ** 2)
                    for _ in range(256):
                        asset.write(block)
            client.send("HeapProfiler.collectGarbage")
            before = client.send("Runtime.getHeapUsage")
            sent = self.upload("chatgpt", path)
            after = client.send("Runtime.getHeapUsage")
            self.assertGreater(path.stat().st_size, 256 * 1024 ** 2)
        read = self.page.evaluate("window.bytesRead")
        growth = sum(after.get(key, 0) - before.get(key, 0) for key in ("usedSize", "backingStorageSize"))
        self.assertLess(read, 128 * 1024)
        self.assertLess(growth, 32 * 1024 ** 2)
        self.assertLess(len(sent), 1024)
        with ZipFile(BytesIO(sent)) as archive:
            self.assertEqual(archive.namelist(), ["conversations.json"])
        print(f"256 MiB disk ZIP: {read} bytes read by JS, {len(sent)} bytes uploaded, {growth} bytes heap growth")

    def test_finished_jobs_are_collapsed_and_retries_stay_visible(self):
        for identifier, status, failed in (
            ("done", "completed", 0), ("retry", "completed", 1), ("preview", "preview", 0),
            ("queued", "queued", 0), ("running", "running", 0), ("paused", "failed", 0),
        ):
            self.jobs.append(dict(id=identifier, source="deepseek", status=status,
                created_at="2026-11-18T12:00:00+08:00", model_calls=1,
                progress=dict(total=1, running=int(status == "running"), failed=failed, processed=int(identifier == "done"))))
        self.page.goto(ORIGIN)
        summary = self.page.get_by_text("历史导入（1）", exact=True)
        expect(summary).to_be_visible()
        self.assertEqual(self.page.locator("#import-jobs > [data-import-job]").count(), 5)
        done = self.page.locator('[data-import-job="done"]')
        expect(done).to_be_hidden()
        expect(self.page.get_by_role("button", name="重试失败的 1 条", exact=True)).to_be_visible()
        summary.click()
        expect(done).to_be_visible()
        with self.page.expect_response(ORIGIN + "/imports"):
            self.page.get_by_role("button", name="刷新", exact=True).click()
        expect(done).to_be_visible()

    def test_discard_and_end_confirm_once_and_refresh_the_lists(self):
        for identifier, status in (("unconfirmed", "preview"), ("paused", "failed")):
            self.jobs.append(dict(id=identifier, source="deepseek", status=status,
                created_at="2026-11-18T12:00:00+08:00", model_calls=0,
                progress=dict(total=2, running=0, failed=0, processed=0)))
        self.page.goto(ORIGIN)
        preview = self.page.locator("#import-preview")
        self.page.get_by_role("button", name="继续预览", exact=True).click()
        expect(preview).to_be_visible()
        discard = self.page.get_by_role("button", name="放弃", exact=True)
        finish = self.page.get_by_role("button", name="结束", exact=True)

        def dismiss(dialog):
            messages.append(dialog.message)
            dialog.dismiss()

        def accept(dialog):
            messages.append(dialog.message)
            dialog.accept()

        messages = []
        self.page.once("dialog", dismiss)
        discard.click()
        self.assertEqual(len(messages), 1)
        self.assertEqual(self.actions, [])
        expect(preview).to_be_visible()
        self.page.once("dialog", accept)
        discard.click()
        expect(preview).to_be_hidden()
        expect(discard).to_have_count(0)
        self.assertEqual(len(messages), 2)

        self.page.once("dialog", accept)
        finish.click()
        history = self.page.locator("#import-history")
        expect(history.locator("summary")).to_have_text("历史导入（1）")
        ended = history.locator('[data-import-job="paused"]')
        expect(ended).to_be_hidden()
        history.locator("summary").click()
        expect(ended).to_be_visible()
        expect(ended).to_contain_text("已结束")
        expect(ended.get_by_role("button")).to_have_count(0)
        self.assertEqual(len(messages), 3)
        self.assertIn("重新上传同一份文件", messages[-1])
        self.assertIn("已导过的部分会自动跳过", messages[-1])
        self.assertEqual(self.actions, [("unconfirmed", "discard"), ("paused", "end")])


if __name__ == "__main__":
    unittest.main()

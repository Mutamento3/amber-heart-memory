"""Disposable browser acceptance with iPhone geometry and explicit safe-area insets."""

import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request
from zipfile import ZIP_DEFLATED, ZipFile

from playwright.sync_api import sync_playwright, expect

ORIGIN = "http://localhost:8080"
PASSWORD = "synthetic acceptance password"


def confirm_action(page, action, message, *, accept=True):
    def handle_dialog(dialog):
        if accept:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", handle_dialog)
    try:
        with page.expect_event("dialog") as confirmation:
            action()
        assert confirmation.value.type == "confirm"
        assert message in confirmation.value.message
    finally:
        page.remove_listener("dialog", handle_dialog)


def navigation_cache(page):
    configuration = re.compile(r"/api/spaces/sample/configuration$")
    badge = re.compile(r"/api/spaces/sample/drafts\?status=pending&size=1$")
    settings = page.get_by_role("link", name="设置", exact=True)
    overview = page.get_by_role("link", name="概览", exact=True)
    field = page.locator("#persona-form textarea")
    original = page.request.get(ORIGIN + "/api/spaces/sample/configuration").json()
    held = {"configuration": [], "badge": []}

    def hold_configuration(route):
        held["configuration"].append(route)

    def hold_badge(route):
        held["badge"].append(route)

    def take(name):
        deadline = time.monotonic() + 3
        while not held[name] and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert held[name], f"{name} request did not start"
        return held[name].pop(0)

    def answer(route, data):
        with page.expect_response(route.request.url):
            route.fulfill(json=data)
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")

    def go_overview():
        overview.click()
        expect(page.locator(".hero")).to_be_visible()

    page.route(configuration, hold_configuration)
    page.route(badge, hold_badge)
    try:
        # Both requests must start before either gets a response.
        settings.click()
        pending_badge = take("badge")
        pending_config = take("configuration")
        expect(field).to_have_count(0)
        answer(pending_config, original)
        expect(field).to_have_value(original["persona"])
        pending_badge.fulfill(status=503, json={"detail": "synthetic badge failure"})
        expect(field).to_be_visible()
        page.unroute(badge, hold_badge)
        go_overview()

        page.evaluate("""() => {
            window.warmNavigation = new Promise(resolve => {
                document.addEventListener("click", () => {
                    const start = performance.now();
                    const observer = new MutationObserver(() => {
                        if (!document.querySelector("#persona-form textarea")) return;
                        observer.disconnect();
                        resolve(performance.now() - start);
                    });
                    observer.observe(document.querySelector("#app"), {childList: true, subtree: true});
                }, {capture: true, once: true});
            });
        }""")
        settings.click()
        pending_config = take("configuration")
        expect(field).to_be_visible(timeout=750)
        elapsed_ms = round(page.evaluate("window.warmNavigation"), 1)
        print(f"Mobile navigation: warm settings visible in {elapsed_ms} ms with refresh held", flush=True)
        field.evaluate("(field) => window.cachedPersonaField = field")
        page.wait_for_timeout(750)
        answer(pending_config, original)
        assert field.evaluate("(field) => field === window.cachedPersonaField")

        go_overview()
        settings.click()
        updated = dict(original, persona="模拟后台更新后的人设")
        expect(field).to_have_value(original["persona"])
        answer(take("configuration"), updated)
        expect(field).to_have_value(updated["persona"])
        assert not field.evaluate("(field) => field === window.cachedPersonaField")

        go_overview()
        settings.click()
        pending_config = take("configuration")
        expect(field).to_have_value(updated["persona"])
        field.fill("正在编辑，还没保存")
        answer(pending_config, original)
        expect(field).to_have_value("正在编辑，还没保存")
        field.fill(updated["persona"])
        field.blur()

        go_overview()
        settings.click()
        delayed = take("configuration")
        expect(field).to_have_value(original["persona"])
        go_overview()
        answer(delayed, updated)
        expect(page.locator(".hero")).to_be_visible()
        expect(field).to_have_count(0)
        settings.click()
        expect(field).to_have_value(original["persona"])
        answer(take("configuration"), original)
        go_overview()
    finally:
        page.unroute(configuration, hold_configuration)
        page.unroute(badge, hold_badge)
        for route in held["configuration"] + held["badge"]:
            route.abort()


def access_key_settings(page):
    page.locator('#set-key').scroll_into_view_if_needed()
    page.get_by_role('button', name='生成钥匙', exact=True).click()
    field = page.locator('#space-key')
    expect(field).to_have_attribute('type', 'password')
    original = field.input_value()
    assert original.startswith('mk_')
    expect(page.locator('#mcp-address')).to_have_value(ORIGIN + '/mcp')
    expect(page.locator('#proxy-address')).to_have_value(ORIGIN + '/v1')
    expect(page.locator('#mcp-transport')).to_have_value('Streamable HTTP')
    expect(page.locator('#mcp-header-name')).to_have_value('Authorization')
    expect(page.locator('#mcp-header-value')).to_have_value('Bearer ' + original)
    expect(page.locator('#mcp-header-value')).to_have_attribute('type','password')
    page.get_by_role('button', name='显示全文', exact=True).click()
    expect(field).to_have_attribute('type', 'text')
    page.get_by_role('button', name='收起全文', exact=True).click()
    page.get_by_role('link', name='概览', exact=True).click()
    page.get_by_role('link', name='设置', exact=True).click()
    expect(page.locator('#space-key')).to_have_value(original)
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='重新生成', exact=True).click()
    expect(page.locator('#space-key')).not_to_have_value(original)
    expect(page.locator('#mcp-header-value')).to_have_value('Bearer ' + page.locator('#space-key').input_value())
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='作废钥匙', exact=True).click()
    expect(page.locator('#space-key')).to_have_count(0)
    expect(page.locator('[data-copy-mcp=mcp-header-value]')).to_be_disabled()
    page.get_by_role('button', name='生成钥匙', exact=True).click()
    expect(page.locator('#space-key')).to_have_attribute('type', 'password')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def unfinished_draft_defaults(page):
    collection = page.request.get(ORIGIN + "/api/spaces/sample/drafts?status=pending&size=1").json()
    identifier = collection["items"][0]["id"]
    endpoint = ORIGIN + f"/api/spaces/sample/drafts/{identifier}"
    original = page.request.get(endpoint).json()["draft"]["payload"]
    csrf = page.request.get(ORIGIN + "/api/session").json()["csrf"]
    for status in ("structure_pending", "structure_failed"):
        detail = page.request.get(endpoint).json()
        detail["draft"].update(payload=None, status=status)
        def handler(route):
            if route.request.method == "GET":
                route.fulfill(json=detail)
            else:
                route.continue_()
        pattern = f"**/api/spaces/sample/drafts/{identifier}"
        page.route(pattern, handler)
        try:
            page.get_by_role("link", name="待审核", exact=False).click()
            hint = "还在整理中" if status == "structure_pending" else "可以重新整理"
            expect(page.locator(".draft-card .hint").filter(has_text=hint)).to_be_visible()
            page.get_by_label("主要主题", exact=True).select_option(str(original["topic_ids"][0]))
            expect(page.locator(".draft-form .autosave-status [role=status]")).to_have_text("已保存")
            actual = page.request.get(endpoint).json()["draft"]
            payload = actual["payload"]
            assert payload["source_date"] is None, "business date must not become an event date"
            assert payload["cognitive_status"] == "stable"
            assert payload["decay_layer"] == "tier_4_episode"
            assert payload["valence"] is None and payload["arousal"] is None
            assert payload["entities"] == [] and payload["relations"] == []
            assert payload["evidence"] == detail["draft"]["narrative"]["evidence"]
        finally:
            page.unroute(pattern, handler)
        # Restore the ordinary structured seed for the full review below.
        response = page.request.put(endpoint, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            data=dict(payload=original, expected_version=actual["version"],
                expected_policy_revision=detail["configuration"]["revision"]))
        assert response.ok, response.text()


def open_overview_page(page, heading, path):
    page.get_by_role("link", name="概览", exact=True).click()
    page.locator(f'.overview-entry[data-go="{path}"]').click()
    expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    expect(page.locator('.topbar nav [aria-current=page]')).to_have_text("概览")
    expect(page.get_by_role("link", name="返回概览", exact=True)).to_be_visible()


def diary_pages(page, capture):
    open_overview_page(page, "日记", "diaries")
    expect(page.locator(".diary-row")).to_have_count(2)
    # A contents list: date and weekday plus an opening excerpt; no repeated "日记" labels or intro line.
    expect(page.locator(".diary-note")).to_have_count(0)
    first = page.locator(".diary-summary").first
    expect(first.locator(".diary-date")).to_have_text(re.compile(r"^(\d{4}年)?\d+月\d+日 · 周[日一二三四五六]"))
    expect(first.locator(".diary-excerpt")).to_contain_text("窗边的绿植")
    expect(first).not_to_contain_text("日记")
    first.click()
    expect(page.locator(".diary-body")).to_contain_text("窗边的绿植")
    expect(page.locator(".diary-body")).not_to_contain_text("林舟的日记")
    expect(first.locator(".diary-excerpt")).to_be_hidden()
    # Opening fetches only that diary in place; a failed request offers a retry instead of hanging.
    first.click()
    expect(page.locator(".diary-body")).to_have_count(0)
    detail = re.compile(r"/api/spaces/sample/diaries/\d+$")
    page.route(detail, lambda route: route.abort())
    page.locator(".diary-summary").nth(1).click()
    expect(page.locator(".diary-reader .error")).to_be_visible()
    page.unroute(detail)
    page.locator(".diary-reader").get_by_role("button", name="重试", exact=True).click()
    expect(page.locator(".diary-body")).to_be_visible()
    first.click()
    expect(page.locator(".diary-body")).to_contain_text("窗边的绿植")
    expect(page.locator(".diary-row.open")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    capture("diaries-iphone-webkit.png")
    page.get_by_role("button", name="编辑", exact=True).click()
    field = page.locator("#diary-content")
    field.fill("# 林舟的日记\n浏览器修改后的日记：我们记下了明天的安排。")
    capture("diary-edit-iphone-webkit.png")
    page.get_by_role("link", name="返回概览", exact=True).click()
    page.locator('.overview-entry[data-go="diaries"]').click()
    page.locator(".diary-summary").first.click()
    expect(page.locator(".diary-body")).to_contain_text("浏览器修改后的日记")
    page.get_by_role("link", name="记忆", exact=True).click()
    expect(page.locator(".cardrow").first).to_be_visible()
    expect(page.get_by_role("tablist")).to_have_count(0)


def open_conversation_records(page):
    page.get_by_role('link', name='记录', exact=True).click()
    expect(page.locator('.topbar nav [aria-current=page]')).to_have_text('记录')
    expect(page.get_by_role('heading', name='聊天记录', exact=True)).to_be_visible()
    expect(page.locator('.page-description')).to_have_count(0)
    expect(page.locator('.conversation-list')).to_be_visible()
    expect(page.locator('#summary-library')).to_have_count(0)


def conversation_scrollback(page):
    """Recall history opens at the newest turn and loads older turns on scroll-up."""
    def turn(position, day):
        when = f"{day}T10:{position:02d}:00+08:00"
        return dict(id=position, position_id=position, business_date=day, created_at=when,
                    generation_state="complete", content_deleted_at=None, recall_count=0,
                    summary_ids=None, history_omitted=None, requests=[], tool_calls=[], messages=[
                        dict(id=position * 2, turn_id=position, request_id=None, event_index=0, role="user",
                             content_state="complete", occurred_at=when, display_text=f"合成第{position}轮"),
                        dict(id=position * 2 + 1, turn_id=position, request_id=position, event_index=1,
                             role="assistant", content_state="complete", occurred_at=when,
                             display_text="合成回复\n" * 3)])
    newest = [turn(p, "2026-11-14" if p <= 13 else "2026-11-15") for p in range(11, 31)]
    older = [turn(p, "2026-11-13" if p <= 7 else "2026-11-14") for p in range(6, 11)]
    older[-1]["summarized"] = True
    newest[-2]["summary_progress"] = dict(cycle_count=4, trigger_count=6)
    newest[-1]["summary_progress"] = dict(cycle_count=6, trigger_count=6, status="failed")
    attempt = dict(id="a" * 32, turn_id=30, business_date="2026-11-15", cycle_count=6, trigger_count=6)
    retry_requested = False
    status_reads_after_retry = 0
    requested = []

    def fulfil(route):
        before = "before=" in route.request.url
        requested.append(route.request.url)
        items, more = (older, False) if before else (newest, True)
        route.fulfill(json=dict(items=items, total=25, has_more=more, next_before=items[0]["position_id"], size=20))

    def summary_response(status):
        return dict(status=status, attempt=attempt, covered_turn_ids=[28] if status == "success" else [])

    def read_summary(route):
        nonlocal status_reads_after_retry
        if retry_requested:
            status_reads_after_retry += 1
        route.fulfill(json=summary_response("success" if status_reads_after_retry > 1 else "failed"))

    def retry_summary(route):
        nonlocal retry_requested
        assert route.request.post_data_json == {"attempt_id": attempt["id"]}
        retry_requested = True
        route.fulfill(json=summary_response("generating"))

    listing = re.compile(r"/api/spaces/sample/conversations(\?.*)?$")
    status_path = "**/api/spaces/sample/summaries/status"
    retry_path = "**/api/spaces/sample/summaries/retry"
    page.route(listing, fulfil)
    page.route(status_path, read_summary)
    page.route(retry_path, retry_summary)
    page.goto(ORIGIN + "/conversations")
    expect(page.locator(".conversation-turn")).to_have_count(20)
    expect(page.locator(".pagination")).to_have_count(0)
    assert page.evaluate("innerHeight + scrollY >= document.documentElement.scrollHeight - 2")
    expect(page.locator(".conversation-summary:visible")).to_have_count(2)
    expect(page.locator('[data-turn="29"] .assistant .conversation-summary')).to_have_text("摘要：4/6 条")
    progress = page.locator('[data-turn="30"] .assistant .conversation-summary')
    expect(progress).to_contain_text("摘要：6/6 条")
    progress.get_by_role("button", name="重试生成及时摘要").click()
    expect(progress).to_contain_text("生成中")
    expect(progress.get_by_label("摘要已生成")).to_be_visible()
    expect(page.locator('.conversation-summary-boundary + [data-turn="29"]')).to_have_count(1)
    top = page.evaluate("() => { scrollTo(0, 0); return document.querySelector('.conversation-turn[data-turn=\"11\"]').getBoundingClientRect().top }")
    expect(page.locator(".conversation-turn")).to_have_count(25)
    after = page.evaluate("document.querySelector('.conversation-turn[data-turn=\"11\"]').getBoundingClientRect().top")
    assert abs(after - top) < 2, (top, after)
    # The older batch's last day continues the day already shown: one heading, not two.
    expect(page.locator(".conversation-day > h2")).to_have_count(3)
    expect(page.locator(".conversation-day").nth(0).locator(".conversation-turn")).to_have_count(2)
    expect(page.locator(".conversation-day").nth(1).locator(".conversation-turn")).to_have_count(6)
    expect(page.locator('.conversation-summary-boundary + [data-turn="11"]')).to_have_count(1)
    expect(page.locator(".conversation-more")).to_have_count(0)
    assert any("before=11" in url for url in requested), requested
    page.unroute(listing, fulfil)
    page.unroute(status_path)
    page.unroute(retry_path)


def conversation_delete(page):
    """Select and remove two real preview turns with the iPhone layout."""
    page.goto(ORIGIN + "/conversations")
    toggle = page.get_by_role("button", name="删除", exact=True)
    expect(toggle).to_be_visible()
    list_area = page.locator(".conversation-list")
    first = list_area.locator(".conversation-turn").filter(has_text="窗边的绿植该浇水了吗？")
    second = list_area.locator(".conversation-turn").filter(has_text="帮我写一句给自己的鼓励。")
    circles = list_area.locator(".conversation-select")
    expect(circles.first).not_to_be_visible()
    toggle.click()
    bar = page.locator(".conversation-delete-bar")
    one = first.locator(".conversation-select")
    two = second.locator(".conversation-select")
    expect(bar.get_by_role("button", name="删除 0 轮")).to_be_disabled()
    expect(one).to_have_attribute("aria-pressed", "false")
    one.click()
    two.click()
    expect(one).to_have_attribute("aria-pressed", "true")
    expect(bar.get_by_role("button", name="删除 2 轮")).to_be_enabled()
    assert one.bounding_box()["width"] >= 44
    assert bar.bounding_box()["y"] + bar.bounding_box()["height"] <= page.locator(".topbar nav").bounding_box()["y"] + 1
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    one.click()
    expect(bar.get_by_role("button", name="删除 1 轮")).to_be_enabled()
    bar.get_by_role("button", name="取消", exact=True).click()
    expect(circles.first).not_to_be_visible()
    toggle.click()
    one.click()
    two.click()
    remove = bar.get_by_role("button", name="删除 2 轮")
    remove.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text(
        "删除这 2 轮聊天？TA 不会再看到它们，相关的及时摘要会重新写。"
        "已经整理成记忆卡片或写进日记的内容不会跟着删，请到待审核或记忆里检查。"
    )
    dialog.get_by_role("button", name="取消", exact=True).click()
    expect(remove).to_be_enabled()
    remove.click()
    with page.expect_response(lambda response: response.url.endswith("/conversations/delete")) as result:
        dialog.get_by_role("button", name="删除 2 轮", exact=True).click()
    assert result.value.json() == {"deleted": 2}
    expect(dialog).not_to_be_visible()
    expect(first).to_have_count(0)
    expect(second).to_have_count(0)
    expect(list_area).to_contain_text("接着聊接下来的计划。")


def hosted_pages(page, capture):
    expect(page.locator('.conversation-entry')).to_have_count(0)
    recall_link = page.locator('.today .recall-overview a')
    expect(recall_link).to_have_attribute('href', '/conversations')
    recall_link.click()
    expect(page.get_by_role('heading', name='聊天记录', exact=True)).to_be_visible()
    expect(page.locator('.conversation-list')).to_be_visible()
    page.reload()
    expect(page.get_by_role('heading', name='聊天记录', exact=True)).to_be_visible()
    expect(page.locator('.conversation-list')).to_be_visible()
    page.goto(ORIGIN + '/memories?tab=conversations')
    expect(page).to_have_url(ORIGIN + '/conversations')
    expect(page.locator('.conversation-list')).to_be_visible()
    expect(page.locator('.topbar nav [aria-current=page]')).to_have_text('记录')
    expect(page.locator('.page-description')).to_have_count(0)
    expect(page.locator('#summary-library')).to_have_count(0)
    expect(page.locator(".conversation-turn")).to_have_count(8)
    headings = page.locator('.conversation-day > h2')
    expect(headings).to_have_count(3)
    expect(headings.first).to_have_text(re.compile(r'^\d+月\d+日 周[日一二三四五六]$'))
    expect(page.locator(".conversation-list")).to_contain_text("[图片：合成的窗边绿植截图。]")
    expect(page.locator('.conversation-list')).to_contain_text('未完成')
    expect(page.locator('.conversation-list')).to_contain_text('上游失败 · 未获回应')
    recalled_turn = page.locator('.conversation-turn').filter(has_text='窗边的绿植该浇水了吗？')
    user = recalled_turn.locator('.conversation-message.user').bounding_box()
    ai = recalled_turn.locator('.conversation-message.assistant').bounding_box()
    assert user["x"] > ai["x"], (user, ai)
    detail = recalled_turn.locator('.conversation-message.assistant .conversation-recall')
    expect(detail.locator('summary')).to_have_text('本轮召回 1 张记忆')
    detail.locator('summary').click()
    expect(detail.locator('.recalled-card')).to_have_count(1)
    expect(detail.locator('.receipt-label')).to_have_text('模型报告使用')
    unreported = page.locator('.conversation-turn').filter(has_text='帮我写一句给自己的鼓励。').locator('.conversation-recall')
    unreported.locator('summary').click()
    expect(unreported.locator('.receipt-label')).to_have_text('未报告')
    unused = page.locator('.conversation-turn').filter(has_text='接着聊接下来的计划。').locator('.conversation-recall')
    expect(unused.locator('summary')).to_have_text('本轮召回 1 张记忆')
    unused.locator('summary').click()
    expect(unused.locator('.receipt-label')).to_have_text('模型报告未使用')
    tools = page.locator('.conversation-turn').filter(has_text='帮我查查土豆和打雷。')
    expect(tools.locator('.conversation-tool, .conversation-message.assistant .conversation-body')).to_have_text([
        '搜索记忆「土豆」 · 没找到', '搜索记忆「打雷」 · 没找到', '我换了两个词查，还是没找到。',
    ])
    expect(tools.locator('.conversation-message.assistant')).to_have_count(1)
    expect(tools.locator('.conversation-recall')).to_have_count(0)
    summarized = page.locator('.conversation-turn').filter(has_text='接着聊接下来的计划。')
    expect(summarized.locator(".history-omitted")).to_contain_text("已告诉 TA")
    # Summaries live on their own page; the chat view does not list them per turn.
    expect(summarized.locator(".round-summaries")).to_have_count(0)
    # The assistant is shown by its configured name, not a generic "AI".
    expect(page.locator(".conversation-message.assistant .conversation-heading span").first).not_to_have_text("AI")
    assert '<used>' not in page.locator('.conversation-list').inner_text()
    assert page.locator('.conversation-list textarea, .conversation-list input').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    capture('conversations-iphone-webkit.png')
    open_overview_page(page, "及时摘要", "summaries")
    library = page.locator("#summary-library")
    # One fold per day with the newest open; each summary shows its time range and text directly.
    days = library.locator("details.summary-day")
    expect(days.first).to_have_attribute("open", "")
    expect(days.first.locator("summary")).to_contain_text(re.compile(r"\d+月\d+日 · 周[日一二三四五六]"))
    expect(days.first.locator("summary")).not_to_contain_text("份")
    expect(library.locator(".summary-entry").first).to_be_visible()
    expect(library.locator(".summary-entry details")).to_have_count(0)
    expect(library.locator(".summary-entry .summary-when").first).to_have_text(re.compile(r"^\d{2}:\d{2}–\d{2}:\d{2}$"))
    expect(library.locator(".summary-text").first).to_contain_text("合成二次摘要")
    expect(library.locator(".summary-retention")).to_have_text("只保留最近 15 天，更早的日子可以看日记。")
    capture("summaries-iphone-webkit.png")
    page.get_by_role('link', name='设置', exact=True).click()
    persona = page.locator('#persona-text')
    persona.fill('你是一位耐心的助手。\n尊重用户的表达，用清楚、自然的话交流。')
    expect(page.locator('#persona-form [role=status]')).to_have_text('已保存')
    expect(page.locator('#persona-text')).to_be_focused()
    page.locator('#persona-text').blur()
    open_conversation_records(page)
    page.get_by_role('link', name='设置', exact=True).click()
    expect(page.locator('#persona-text')).to_have_value('你是一位耐心的助手。\n尊重用户的表达，用清楚、自然的话交流。')
    page.locator('#set-persona').scroll_into_view_if_needed()
    capture('persona-iphone-webkit.png')
    # Saving persona must preserve unsaved edits in another settings form.
    page.locator('[name=user_name]').fill('尚未保存的参与者')
    page.locator('#persona-text').fill('')
    expect(page.locator('#persona-form [role=status]')).to_have_text('已保存')
    expect(page.locator('#persona-text')).to_be_focused()
    page.locator('#persona-text').blur()
    dialogs = []
    def dismiss(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()
    page.on('dialog', dismiss)
    page.get_by_role('link', name='概览', exact=True).click()
    page.remove_listener('dialog', dismiss)
    assert dialogs
    expect(page.locator('[name=user_name]')).to_have_value('尚未保存的参与者')
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('link', name='概览', exact=True).click()


def persona_imports(page):
    button = page.get_by_role("button", name="从文件导入", exact=True)
    persona = page.locator("#persona-text")
    status = page.locator("#persona-form [role=status]")
    error = page.locator("#persona-import-error")
    configuration = ORIGIN + "/api/spaces/sample/configuration"
    expect(page.get_by_text("支持 .txt、.md、.docx", exact=True)).to_be_visible()
    expect(persona).to_have_value("")

    def choose(name, mime_type, data, *, replace=None):
        with page.expect_file_chooser() as selection:
            button.click()
        chooser = selection.value
        payload = {"name": name, "mimeType": mime_type, "buffer": data}
        if replace is None:
            chooser.set_files(payload)
        else:
            confirm_action(page, lambda: chooser.set_files(payload), "会替换当前人设，继续吗？", accept=replace)
        expect(button).to_be_enabled()

    plain = "请自然交谈。\n尊重用户。"
    choose("persona.txt", "text/plain", plain.replace("\n", "\r\n").encode("utf-8-sig"))
    expect(persona).to_have_value(plain)
    expect(status).to_have_text("已保存")
    plain_config = page.request.get(configuration).json()
    assert plain_config["persona"] == plain

    word = BytesIO()
    with ZipFile(word, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
          <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
          <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
        </Types>""")
        archive.writestr("_rels/.rels", """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
        </Relationships>""")
        archive.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p><w:r><w:t xml:space="preserve">第一段 &amp; </w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>纯文字</w:t></w:r></w:p>
            <w:p/>
            <w:p><w:r><w:t>第二段</w:t><w:tab/><w:t>末尾</w:t></w:r></w:p>
          </w:body>
        </w:document>""")
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    choose("persona.docx", docx_type, word.getvalue(), replace=False)
    expect(persona).to_have_value(plain)
    assert page.request.get(configuration).json()["revision"] == plain_config["revision"]
    choose("persona.docx", docx_type, word.getvalue(), replace=True)
    imported = "第一段 & 纯文字\n\n第二段\t末尾"
    expect(persona).to_have_value(imported)
    expect(status).to_have_text("已保存")
    assert page.request.get(configuration).json()["persona"] == imported

    for name, mime_type, data, reason in (
        ("old.doc", "application/msword", b"old", "请在 Word 里另存为 .docx 再导入"),
        ("broken.docx", docx_type, b"invalid", "无法读取 .docx 正文"),
        ("too-long.txt", "text/plain", ("字" * 100001).encode(), "导入内容超过 10 万字"),
    ):
        choose(name, mime_type, data)
        expect(error).to_contain_text(reason)
        expect(persona).to_have_value(imported)
    assert page.request.get(configuration).json()["persona"] == imported

    markdown = "# 标题\n**保留 Markdown 原文**"
    choose("persona.md", "text/markdown", markdown.encode(), replace=True)
    expect(persona).to_have_value(markdown)
    expect(status).to_have_text("已保存")
    assert page.request.get(configuration).json()["persona"] == markdown
    persona.fill("")
    persona.blur()
    expect(status).to_have_text("已保存")


def auto_approval_settings(page, capture):
    page.get_by_role('link',name='设置',exact=True).click()
    flag=page.locator('#auto-approval')
    expect(flag).to_be_checked()
    expect(page.locator('#review-mode')).to_have_count(0)
    flag.uncheck()
    expect(flag).not_to_be_checked()
    expect(page.locator('#auto-approval-form [role=status]')).to_have_text('已保存')
    expect(page.locator('.topbar nav a')).to_have_count(5)
    expect(page.locator('.topbar nav [data-page=review]')).to_be_visible()
    page.get_by_role('link',name='概览',exact=True).click()
    expect(page.locator('.approval-schedule')).to_contain_text('仅手动收录')
    page.get_by_role('link',name='设置',exact=True).click()
    expect(page.locator('#auto-approval')).not_to_be_checked()
    capture('auto-approval-iphone-webkit.png')
    page.locator('#auto-approval').check()
    expect(page.locator('#auto-approval-form [role=status]')).to_have_text('已保存')
    page.get_by_role('link',name='概览',exact=True).click()
    expect(page.locator('.approval-schedule')).to_contain_text('预计')
    expect(page.locator('.approval-schedule time')).to_have_count(1)
    capture('pending-approval-overview-iphone-webkit.png')
    page.get_by_role('link',name='设置',exact=True).click()
    page.locator('#participants-form').wait_for()


def settings_unsaved_edits(page):
    page.get_by_role("link", name="设置", exact=True).click()
    participants = page.locator("#participants-form")
    expect(participants.get_by_label("又名（如小名）", exact=True)).to_have_count(2)
    expect(participants.locator("[name=user_aliases]")).to_have_attribute("placeholder", "例：满满、小满")
    expect(participants.locator("[name=assistant_aliases]")).to_have_attribute("placeholder", "例：阿言、言言")
    expect(participants).to_contain_text(
        "可以填多个，用顿号隔开。聊天里出现这些名字时，记忆系统会知道说的是同一个人。想让 TA 怎么称呼你，请写在人设或核心记忆里。"
    )
    alias = participants.locator("[name=user_aliases]")
    original_alias = alias.input_value()
    alias.fill(participants.locator("[name=assistant_name]").input_value())
    participants.get_by_role("button", name="保存参与者", exact=True).click()
    expect(participants.locator(".error")).to_have_text(
        "两个人的名字或又名有重复，或者填了宝宝、老婆这类两个人都可能用的爱称。爱称不用填在这里，想让 TA 这么叫你，写在人设或核心记忆里。"
    )
    alias.fill(original_alias)

    def protected():
        dialogs = []

        def dismiss(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", dismiss)
        try:
            page.get_by_role("link", name="概览", exact=True).click()
            assert dialogs, "settings edits were discarded without confirmation"
            expect(page).to_have_url(re.compile(r"/settings$"))
        finally:
            page.remove_listener("dialog", dismiss)

    date = page.locator("#together-form [name=acquaintance_date]")
    date.fill("2024-04-01")
    # The phone's native date text is replaced by a formal Chinese date in ink color.
    expect(page.locator("#together-form .date-display")).to_have_text("2024年4月1日")
    page.locator("[name=user_name]").fill("合成验收用户")
    page.locator("[name=user_name]").blur()
    expect(page.locator("#together-form [role=status]")).to_have_text("已保存")
    protected()
    date.fill("2024-03-01")
    page.get_by_role("button", name="保存参与者", exact=True).click()
    page.get_by_role("status").filter(has_text="参与者已保存").wait_for()
    expect(page.locator("#together-form [role=status]")).to_have_text("已保存")
    page.get_by_role("button", name="清除", exact=True).click()
    expect(page.locator("#together-form [role=status]")).to_have_text("已保存")
    expect(page.locator("#together-form .date-display")).to_have_text("选择日期")
    page.get_by_role("link", name="概览", exact=True).click()
    page.wait_for_url("**/overview")


def review_queue_list(page, capture):
    expect(page.locator('.topbar nav [data-page=review]')).to_be_visible()
    expect(page.locator('.topbar nav [data-page=review] .badge')).to_have_count(0)
    capture('navigation-empty-iphone-webkit.png')
    # Distinct fixed HTTP cards exercise per-card edits and actions without mutating seed data.
    processed = page.request.get(ORIGIN + "/api/spaces/sample/drafts?status=processed&size=1").json()
    identifier = processed["items"][0]["id"]
    original = page.request.get(ORIGIN + f"/api/spaces/sample/drafts/{identifier}").json()
    remaining = [9001, 9002, 9003]
    saved = {}

    def listing(route):
        query = parse_qs(urlsplit(route.request.url).query)
        number = int(query.get("page", [1])[0])
        size = int(query.get("size", [20])[0])
        selected = remaining if query.get("all_items") == ["true"] else remaining[(number-1)*size:number*size]
        rows = [dict(id=item, title=f"逐条验收草稿 {item}", status="pending",
            business_date="2026-10-29") for item in selected]
        route.fulfill(json=dict(items=rows, total=len(remaining), size=size, page=number))

    def detail(route):
        item = int(urlsplit(route.request.url).path.rsplit("/", 1)[1])
        if item not in saved:
            data = deepcopy(original)
            data["draft"].update(id=item, status="pending", version=1,
                policy_revision=data["configuration"]["revision"], memory_item_id=None,
                review_key=None, duplicate_memory_id=None)
            for field in ("payload", "narrative"):
                data["draft"][field]["title"] = f"逐条验收草稿 {item}"
            if item == 9003:
                data["draft"].update(status="structure_failed", payload=None, error_code="structure:timeout")
            saved[item] = data
        draft = saved[item]["draft"]
        if route.request.method == "PUT":
            body = route.request.post_data_json
            assert body["expected_version"] == draft["version"]
            draft.update(payload=body["payload"], version=draft["version"] + 1)
            route.fulfill(json=draft)
        else:
            route.fulfill(json=saved[item])

    def finish(route):
        item = int(urlsplit(route.request.url).path.split("/")[-2])
        remaining.remove(item)
        assert route.request.post_data_json["expected_version"] == saved[item]["draft"]["version"]
        if route.request.url.endswith("/approve"):
            assert saved[item]["draft"]["payload"]["title"] == "收录前的最新修改"
            route.fulfill(json={"status": "approved", "review_key": route.request.post_data_json["request_key"]})
        else:
            route.fulfill(json={"status": "rejected"})

    def restructure(route):
        draft=saved[9003]["draft"]
        assert route.request.post_data_json["expected_version"] == draft["version"]
        draft.update(version=2, status="pending", error_code=None,
            payload={**original["draft"]["payload"], "title":draft["narrative"]["title"]})
        route.fulfill(status=202,json={"version":1,"wait_seconds":5})

    routes = [
        (re.compile(r"/api/spaces/sample/drafts/9003/restructure$"), restructure),
        ("**/api/spaces/sample/drafts?*", listing),
        (re.compile(r"/api/spaces/sample/drafts/900[123]$"), detail),
        (re.compile(r"/api/spaces/sample/drafts/900[123]/(?:reject|approve)$"), finish),
    ]
    for pattern, handler in routes:
        page.route(pattern, handler)
    try:
        page.get_by_role("link", name="待审核", exact=False).click()
        expect(page.locator(".draft-card")).to_have_count(3)
        expect(page.locator(".review-navigation")).to_have_count(0)
        first=page.locator('[data-draft-id="9001"]')
        second=page.locator('[data-draft-id="9002"]')
        failed=page.locator('[data-draft-id="9003"]')
        expect(failed).to_contain_text("整理时模型超时了")
        assert first.bounding_box()["y"] < second.bounding_box()["y"] < failed.bounding_box()["y"]
        first.locator("[name=title]").fill("收录前的最新修改")
        second.locator("[name=title]").fill("同时编辑第二张")
        failed.get_by_role("button", name="重新整理", exact=True).click()
        expect(failed.get_by_role("button", name="重新整理", exact=True)).to_have_count(0)
        expect(failed.locator("[name=title]")).to_have_value("逐条验收草稿 9003")
        expect(second.locator("[name=title]")).to_have_value("同时编辑第二张")
        capture('review-list-iphone-webkit.png')
        first.get_by_role("button", name="确认收录", exact=True).click()
        expect(first).to_have_count(0)
        expect(second.locator("[name=title]")).to_have_value("同时编辑第二张")
        for item in (failed,second):
            confirm_action(page,item.get_by_role("button", name="拒绝", exact=True).click,"拒绝这张草稿")
            expect(item).to_have_count(0)
        expect(page.locator(".draft-card")).to_have_count(0)
        expect(page.locator(".reader")).to_contain_text("草稿都审完了")
        expect(page.locator('[data-page="review"] .badge')).to_have_count(0)
    finally:
        for pattern, handler in routes:
            page.unroute(pattern, handler)
    expect(page.locator('.topbar nav [data-page=review]')).to_be_visible()
    expect(page.locator('.topbar nav a')).to_have_count(5)


def settings_space_switch(page):
    page.get_by_role("link", name="设置", exact=True).click()
    selector = page.get_by_label("记忆空间", exact=True)
    expect(selector).to_be_visible()
    assert page.locator(".pagehead select").count() == 0
    selector.select_option("second")
    expect(page.locator("[name=assistant_name]")).to_have_value("远山")
    endpoint = ORIGIN + "/api/spaces/sample/configuration"
    held = []

    def handler(route):
        held.append(route)

    page.route(endpoint, handler)
    try:
        with page.expect_request(endpoint):
            selector.select_option("sample")
        # Returning to a previous space starts with a fresh read as well.
        expect(page.locator("#main .loading")).to_be_visible()
        expect(page.locator("[name=assistant_name]")).to_have_count(0)
        held.pop().continue_()
    finally:
        page.unroute(endpoint, handler)
    expect(page.locator("[name=assistant_name]")).to_have_value("林舟")
    # A single-space instance has no selector, including on its settings page.
    def one_space(route):
        route.fulfill(json=[{"id": "sample", "name": "林舟"}])
    page.route("**/api/spaces", one_space)
    try:
        page.reload()
        page.locator("#participants-form").wait_for()
        assert page.locator("#space").count() == 0
    finally:
        page.unroute("**/api/spaces", one_space)
    page.reload()
    page.locator("#participants-form").wait_for()


def card_mutations(page, capture, suffix):
    page.get_by_role('link',name='记忆',exact=True).click()
    row=page.locator('.cardrow').first
    row.wait_for()
    mid=row.get_attribute('data-card')
    endpoint=f'{ORIGIN}/api/spaces/sample/cards/{mid}'
    before=page.request.get(endpoint).json()
    row.locator('summary').click()
    row.get_by_role('button',name='修改',exact=True).click()
    form=row.locator('.card-editor')
    title=form.locator('[name=title]')
    expect(form.locator('textarea')).to_have_count(1)
    expect(form.locator('[name=content],[name=summary],[name=source_date],[name=experience_mode]')).to_have_count(0)
    changed='合成星图 · '+('手机' if suffix.startswith('iphone') else '桌面')

    def unavailable(route):
        if route.request.method in ('PATCH','DELETE'):
            route.fulfill(status=503,json={'detail':'合成暂时不可用'})
        else:
            route.continue_()
    page.route(endpoint,unavailable)
    title.fill(changed)
    form.locator('[name=is_milestone]').check()
    options=form.get_by_label('主要主题').locator('option').evaluate_all('items=>items.filter(x=>!x.disabled&&x.value).map(x=>x.value)')
    form.get_by_label('主要主题').select_option(options[-1])
    expect(form.locator('.autosave-status [role=status]')).to_have_text('保存失败（合成暂时不可用）')
    expect(title).to_have_value(changed)
    assert page.request.get(endpoint).json()['title']==before['title']
    page.once('dialog',lambda dialog:dialog.dismiss())
    page.get_by_role('link',name='概览',exact=True).click()
    expect(title).to_have_value(changed)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    form.locator('.card-actions').scroll_into_view_if_needed()
    capture('card-editor-'+suffix+'.png')
    page.unroute(endpoint,unavailable)
    form.get_by_role('button',name='重试',exact=True).click()
    expect(form.locator('.autosave-status [role=status]')).to_have_text('已保存')
    form.get_by_role('button',name='完成编辑',exact=True).click()
    row=page.locator(f'.cardrow[data-card="{mid}"]')
    expect(row.locator('h2')).to_have_text(changed)
    expect(row.locator('.cardbody .meta')).to_contain_text('里程碑')
    saved=page.request.get(endpoint).json()
    for key in ('content','summary','source_date','experience_mode'):
        assert saved[key]==before[key],key
    assert saved['title']==changed and saved['primary_topic_id']==int(options[-1])
    expect(row.locator('.card-content')).to_have_text(before['content'])
    capture('card-saved-'+suffix+'.png')
    page.once('dialog',lambda dialog:dialog.dismiss())
    row.get_by_role('button',name='删除',exact=True).click()
    assert page.request.get(endpoint).ok
    page.route(endpoint,unavailable)
    page.once('dialog',lambda dialog:dialog.accept())
    row.get_by_role('button',name='删除',exact=True).click()
    expect(row.locator('.card-error')).to_contain_text('删除未完成')
    assert page.request.get(endpoint).ok
    page.unroute(endpoint,unavailable)
    page.once('dialog',lambda dialog:dialog.accept())
    row.get_by_role('button',name='删除',exact=True).click()
    expect(row).to_have_count(0)
    assert not page.request.get(endpoint).ok
    page.get_by_role('button',name='筛选',exact=True).click()
    page.locator('[name=archive]').select_option('archived')
    month=page.locator('[name=archive_month]')
    expect(month).to_be_visible()
    month.select_option('2026-10')
    expect(page.locator('[data-filter-key=archive_month]')).to_contain_text('2026-10')
    archived=page.locator('.cardrow').first
    archived.locator('summary').click()
    expect(archived.locator('.cardbody .meta')).to_contain_text('已归档')
    archived.get_by_role('button',name='修改',exact=True).click()
    expect(archived.locator('.card-editor')).to_be_visible()
    archived.get_by_role('button',name='完成编辑',exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    capture('archived-cards-'+suffix+'.png')
    archived_id=archived.get_attribute('data-card')
    archived_endpoint=f'{ORIGIN}/api/spaces/sample/cards/{archived_id}'
    archived_before=page.request.get(archived_endpoint).json()
    assert archived_before['archived_at'].startswith('2026-10')
    restore_endpoint=archived_endpoint+'/restore'
    def failed_restore(route):
        route.fulfill(status=503,json={'detail':'合成恢复暂时不可用'})
    page.route(restore_endpoint,failed_restore)
    archived.get_by_role('button',name='恢复',exact=True).click()
    expect(archived.locator('.card-error')).to_contain_text('暂时不可用')
    assert page.request.get(archived_endpoint).json()['archive_reason']=='decay'
    page.unroute(restore_endpoint,failed_restore)
    archived.get_by_role('button',name='恢复',exact=True).click()
    expect(page.locator(f'.cardrow[data-card="{archived_id}"]')).to_have_count(0)
    assert page.request.get(archived_endpoint).json()['archive_reason'] is None
    page.locator('[name=archive]').select_option('active')
    expect(month).to_be_hidden()
    restored_row=page.locator(f'.cardrow[data-card="{archived_id}"]')
    restored_row.locator('summary').click()
    expect(restored_row.locator('.card-content')).to_have_text(archived_before['content'])
    expect(restored_row.get_by_role('button',name='恢复',exact=True)).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    capture('restored-card-'+suffix+'.png')


def topic_summary_pages(page,capture,suffix):
    page.get_by_role("link",name="设置",exact=True).click()
    page.get_by_label("记忆空间",exact=True).select_option("summary-"+("mobile" if suffix.startswith("iphone") else "desktop"))
    expect(page.locator("[name=assistant_name]")).to_have_value("合成园丁")
    page.get_by_role("link",name="概览",exact=True).click()
    expect(page.locator(".monthly-summaries")).to_contain_text("未配置")
    page.get_by_role("link",name="待审核",exact=False).click()
    summaries=page.locator(".summary-draft")
    expect(summaries).to_have_count(2)
    expect(page.locator(".summary-draft textarea")).to_have_count(0)
    expect(page.locator(".summary-draft input[type=checkbox]")).to_have_count(0)
    page.locator(".summary-sources summary").first.click()
    expect(page.locator(".summary-sources").first).to_contain_text("2 张")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    capture("topic-summary-review-"+suffix+".png")
    summaries.first.get_by_role("button",name="确认生效",exact=True).click()
    page.get_by_role("status").filter(has_text="总结已生效").wait_for()
    expect(summaries).to_have_count(1)
    page.once("dialog",lambda dialog:dialog.accept())
    page.get_by_role("button",name="驳回",exact=True).click()
    page.get_by_role("status").filter(has_text="总结已驳回").wait_for()
    page.get_by_role("link",name="记忆",exact=True).click()
    page.locator("#toggle-filters").click()
    page.locator('[name=kind]').select_option("topic_summary")
    expect(page.locator(".cardrow")).to_have_count(2)
    page.locator(".cardrow").first.locator("summary").click()
    page.get_by_role("button",name="查看吸收清单",exact=True).click()
    page.locator(".summary-sources summary").first.click()
    expect(page.locator(".cardbody .summary-sources").first).to_contain_text("2 张")
    expect(page.locator(".edit-card")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    capture("topic-summary-effective-"+suffix+".png")
    page.once("dialog",lambda dialog:dialog.accept())
    page.get_by_role("button",name="整篇撤销",exact=True).click()
    page.get_by_role("status").filter(has_text="总结已撤销").wait_for()
    expect(page.locator(".cardrow")).to_have_count(1)
    page.locator('[name=archive]').select_option("archived")
    expect(page.locator(".cardrow")).to_have_count(1)
    page.locator(".cardrow").first.locator("summary").click()
    expect(page.get_by_role("button",name="恢复",exact=True)).to_be_visible()
    capture("topic-summary-archived-"+suffix+".png")
    page.get_by_role("button",name="恢复",exact=True).click()
    page.get_by_role("status").filter(has_text="卡片已恢复").wait_for()
    expect(page.locator(".cardrow")).to_have_count(0)
    page.get_by_role("link",name="设置",exact=True).click()
    page.get_by_label("记忆空间",exact=True).select_option("sample")
    expect(page.locator("[name=assistant_name]")).to_have_value("林舟")


def memos_page(page, capture, suffix, *, pending_seeded=False):
    csrf = page.request.get(ORIGIN + '/api/session').json()['csrf']
    headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
    endpoint = ORIGIN + '/api/spaces/sample/recent-notes'
    original = dict(period='',
        health='2026年11月5日 复查后继续按医嘱用药，两周后复诊。\n2026年11月5日 医生说两周后复诊，这段时间注意休息。',
        longterm='2026年10月18日 开始准备资格考试，每周末复习两章。\n2026年11月2日 报名了春季课程，月底前确认上课时间。',
        daily='2026年11月8日 把绿植搬到了窗边，约好周末一起换盆。')
    before = page.request.get(endpoint).json()
    response = page.request.put(endpoint, headers=headers, data=dict(expected_version=before['version'], sections=original))
    assert response.status == 200, response.text()
    open_overview_page(page, "核心记忆", "core-memory")
    expect(page.locator('#core-memory')).to_be_visible()
    expect(page.locator('#pending-bookmarks')).to_have_count(0)
    expect(page.locator('.topbar nav a')).to_have_count(5)
    # Frontend calculation preserves untouched text and orders only dated slots.
    computed = page.evaluate(r"""async () => {
      const {tidyPreview, count} = await import('./static/memos.js');
      const job = {base_sections: {period: '\n untouched \n', health: '★甲\n乙', longterm: '2026年1月1日 旧\n日期未知\n2026年2月1日 新', daily: ''},
        entries: {'健康1': {section: 'health', index: 0, text: '★甲'}, '健康2': {section: 'health', index: 1, text: '乙'},
          '长期1': {section: 'longterm', index: 0, text: '2026年1月1日 旧'}, '长期2': {section: 'longterm', index: 1, text: '日期未知'}, '长期3': {section: 'longterm', index: 2, text: '2026年2月1日 新'}},
        suggestions: [{id: 'merge', section: 'health', targets: ['健康2','健康1'], type: '合并', new_text: '★合并'},
          {id: 'trim', section: 'longterm', targets: ['长期1'], type: '精简', new_text: '2026年1月1日 精简'}], need_confirm: []};
      return {empty: tidyPreview(job, new Set()).sections, chosen: tidyPreview(job, new Set(['merge','trim'])).sections, count: count('甲 😀 \n乙')};
    }""")
    assert computed['chosen']['period'] == '\n untouched \n'
    assert computed['chosen']['health'] == '★合并'
    assert computed['chosen']['longterm'].split('\n') == ['2026年2月1日 新','日期未知','2026年1月1日 精简']
    assert computed['empty']['health'] == '★甲\n乙'
    assert computed['count'] == 3
    core = page.locator('#core-memory')
    core.fill('小禾喜欢温热的茶。\n重要安排先一起商量。')
    expect(page.locator('#core-form [role=status]')).to_have_text('已保存')
    # The text grows with its content (no inner scrolling) and the count line stays in view.
    core.fill('\n'.join(f'第 {i} 条：小禾喜欢温热的茶。' for i in range(60)))
    expect(page.locator('#core-form [role=status]')).to_have_text('已保存')
    assert core.evaluate("e => e.scrollHeight <= e.clientHeight + 2")
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    head = page.locator('.core-head').bounding_box()
    assert 0 <= head['y'] <= 80, head
    expect(page.locator('.core-head .edit-hint')).to_have_text('点击空白处可编辑')
    core.fill('小禾喜欢温热的茶。\n重要安排先一起商量。\n工作日晚上更想安静地聊天。')
    page.get_by_role('link', name='返回概览', exact=True).click()
    page.locator('.overview-entry[data-go="bookmarks"]').click()
    expect(page.locator('#organized-bookmarks .memo-section.card')).to_have_count(4)
    organized_tab = page.get_by_role('tab', name='已整理', exact=True)
    pending_tab = page.locator('#pending-tab')
    expect(organized_tab).to_have_attribute('aria-selected', 'true')
    expect(pending_tab).to_have_text('待整理 · 1' if pending_seeded else '待整理')
    expect(page.locator('#pending-panel')).to_be_hidden()
    # No heading repeating the tab name: one toolbar line, then one numbered card per section.
    expect(page.get_by_role("heading", name="已整理的书签", exact=True)).to_have_count(0)
    header = page.locator('.memo-recent > .memo-bar')
    expect(header.locator('#notes-count')).to_have_text(re.compile(r'^\d+ 字$'))
    expect(header).to_contain_text('每轮聊天都会带给 TA')
    expect(header.get_by_role('button', name='秘书整理')).to_be_visible()
    expect(page.locator('#reload-notes')).to_be_hidden()
    expect(page.locator('#starred-note-help')).to_be_hidden()
    period = page.locator('[data-section=period]')
    expect(period.get_by_role('heading', name='月经周期')).to_be_visible()
    expect(period.get_by_role('button', name='＋ 添加', exact=True)).to_be_visible()
    expect(period.locator('.memo-rows')).to_be_empty()
    expect(period.locator('.memo-no')).to_have_text('01')
    expect(period.locator('.memo-section-count')).to_have_text('0 字')
    assert period.locator('.memo-rows').evaluate("e => getComputedStyle(e, '::before').content") == '"还没有"'
    expect(page.locator('#core-memory')).to_have_count(0)
    assert '工作日晚上更想安静地聊天。' in page.request.get(ORIGIN + '/api/spaces/sample/configuration').json()['core_memory']
    organized_tab.press('ArrowRight')
    expect(pending_tab).to_be_focused()
    expect(pending_tab).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#organized-bookmarks')).to_be_hidden()
    if pending_seeded:
        expect(page.locator('#pending-help')).to_have_text('共 1 条 · 今晚整理后并入已整理，之前可以撤回')
        # Grouped like the organized tab: a numbered card per section that has bookmarks.
        group = page.locator('.pending-section')
        expect(group).to_have_count(1)
        expect(group.locator('.memo-no')).to_have_text('04')
        expect(group.get_by_role('heading', name='日常杂记')).to_be_visible()
        expect(group.locator('.memo-section-head .meta')).to_have_text('1 条')
        pending = page.locator('.pending-bookmark')
        # No clock time on the row (the text starts with its own date); withdraw sits beside the text.
        expect(pending.locator('time')).to_have_count(0)
        withdraw = pending.get_by_role('button', name='撤回', exact=True)
        body = pending.locator(':scope > p')
        expect(body).to_contain_text('找到了喜欢的新书')
        assert withdraw.bounding_box()['x'] > body.bounding_box()['x'] + body.bounding_box()['width'] - 1
        capture('memos-pending-' + suffix + '.png')
        confirm_action(page, withdraw.click, '撤回这条书签')
    expect(pending_tab).to_have_text('待整理')
    expect(page.locator('#pending-help')).to_be_hidden()
    expect(page.locator('#pending-bookmarks')).to_have_text('今天还没有新书签', use_inner_text=True)
    pending_tab.press('Home')
    expect(organized_tab).to_be_focused()
    expect(organized_tab).to_have_attribute('aria-selected', 'true')

    def note_menu(row):
        row.get_by_role('button', name='条目操作', exact=True).click()
        menu = row.locator('.memo-note-menu')
        expect(menu).to_be_visible()
        return menu

    longterm = page.locator('[data-section=longterm] .memo-row')
    health = page.locator('[data-section=health] .memo-row')
    daily = page.locator('[data-section=daily] .memo-row')
    expect(longterm.first.locator('.memo-note-menu')).to_be_hidden()
    expect(longterm.nth(1)).to_have_css('border-top-width', '1px')
    expect(longterm.first.locator('.memo-star')).to_have_count(0)
    note_menu(longterm.first).get_by_role('button', name='标星', exact=True).click()
    expect(longterm.first.locator('.memo-star')).to_have_text('★')
    expect(page.locator('#starred-note-help')).to_be_visible()
    menu = note_menu(longterm.first)
    expect(menu.get_by_role('button', name='取消标星', exact=True)).to_be_visible()
    if pending_seeded:
        # Place the menu trigger just above the fixed mobile navigation.
        more = longterm.first.get_by_role('button', name='条目操作', exact=True)
        more.click()
        more.evaluate("""button => window.scrollBy(0, button.getBoundingClientRect().bottom -
            document.querySelector('.topbar nav').getBoundingClientRect().top + 18)""")
        menu = note_menu(longterm.first)
        menu_bounds = menu.bounding_box()
        assert menu_bounds['y'] + menu_bounds['height'] <= page.locator('.topbar nav').bounding_box()['y']
        capture('memos-menu-' + suffix + '.png')
    menu.get_by_role('button', name='取消标星', exact=True).click()
    expect(longterm.first.locator('.memo-star')).to_have_count(0)
    expect(page.locator('#starred-note-help')).to_be_hidden()
    note_menu(health.first).get_by_role('button', name='标星', exact=True).click()

    note_menu(daily).get_by_role('button', name='修改', exact=True).click()
    editor = page.locator('.memo-line-editor textarea')
    expect(editor).to_be_focused()
    expect(page.locator('.memo-line-editor label')).to_have_count(0)
    expect(page.get_by_role('button', name='完成编辑', exact=True)).to_have_count(0)
    editor.fill('一行短文字')
    short_height = editor.bounding_box()['height']
    editor.fill('多行文字\n下一行\n再下一行\n最后一行')
    assert editor.bounding_box()['height'] > short_height
    editor.fill('2026年11月8日 绿植搬到窗边，\n周末一起换盆。')
    pending_tab.click()
    expect(page.locator('#organized-bookmarks')).to_be_hidden()
    organized_tab.click()
    expect(editor).to_have_count(0)
    expect(daily.locator('.memo-text')).to_have_text('2026年11月8日 绿植搬到窗边， 周末一起换盆。')
    status = daily.locator('#notes-status [role=status]')
    expect(status).to_have_text('已保存')
    expect(status).to_be_empty(timeout=4000)

    note_menu(daily).get_by_role('button', name='修改', exact=True).click()
    page.route(endpoint, lambda route: route.fulfill(status=503, json={'detail': '合成保存故障'}), times=1)
    editor.fill('绿植移到窗边，周末一起换盆。')
    header.locator('.meta').click()
    expect(editor).to_have_count(0)
    expect(daily.locator('.memo-text')).to_have_text('绿植移到窗边，周末一起换盆。')
    expect(status).to_contain_text('保存失败（合成保存故障）')
    expect(status).not_to_have_class(re.compile(r'fade-saved'))
    daily.get_by_role('button', name='重试', exact=True).click()
    expect(status).to_have_text('已保存')
    assert page.request.get(endpoint).json()['sections']['daily'] == '绿植移到窗边，周末一起换盆。'

    version = page.request.get(endpoint).json()['version']
    note_menu(daily).get_by_role('button', name='修改', exact=True).click()
    editor.fill('  ')
    header.locator('.meta').click()
    expect(editor).to_have_count(0)
    expect(daily.locator('.memo-text')).to_have_text('绿植移到窗边，周末一起换盆。')
    period.locator('.add-note').click()
    editor.fill(' \n ')
    header.locator('.meta').click()
    expect(period.locator('.memo-row')).to_have_count(0)
    assert page.request.get(endpoint).json()['version'] == version
    period.locator('.add-note').click()
    new_note = '2026年11月9日 本次周期已经结束。'
    editor.fill(new_note)
    header.locator('.meta').click()
    expect(period.locator('.memo-text')).to_have_text(new_note)
    delete_note = note_menu(period.locator('.memo-row')).get_by_role('button', name='删除', exact=True)
    confirm_action(page, delete_note.click, '删除这条书签')
    expect(period.locator('.memo-row')).to_have_count(0)
    deleted_status = period.locator('#notes-status [role=status]')
    expect(deleted_status).to_have_text('已保存')
    expect(deleted_status).to_be_empty(timeout=4000)
    capture('memos-' + suffix + '.png')
    page.get_by_role('link', name='设置', exact=True).click()
    page.locator('#mcp-content .mcp-instructions').wait_for()
    capture('mcp-connection-' + suffix + '.png', page.locator('#set-mcp'))
    open_overview_page(page, "记忆书签", "bookmarks")
    page.locator('#notes-sections').wait_for()
    expect(page.locator('#tidy-panel')).to_be_hidden()
    page.get_by_role('button', name='秘书整理', exact=True).click()
    expect(page.locator('.memo-recent #tidy-panel')).to_be_visible()
    page.locator('#start-tidy').click()
    expect(page.locator('#tidy-panel')).to_contain_text('整理建议', timeout=15000)
    expect(page.locator('.tidy-compare')).not_to_have_count(0)
    expect(page.locator('.tidy-question').first).to_contain_text('复诊日期是否已经确定')
    page.locator('#tidy-none').click()
    expect(page.locator('#adopt-tidy')).to_be_disabled()
    pending_tab.click()
    expect(page.locator('#tidy-panel')).to_be_hidden()
    organized_tab.click()
    expect(page.locator('#tidy-panel')).to_be_visible()
    expect(page.locator('#adopt-tidy')).to_be_disabled()
    page.locator('#tidy-all').click()
    capture('memos-tidy-' + suffix + '.png', page.locator('#tidy-panel'))
    page.locator('#adopt-tidy').click()
    expect(page.locator('#adoption-notice')).to_be_visible()
    expect(page.locator('#tidy-panel')).to_be_hidden()
    expect(page.locator('.memo-changed')).not_to_have_count(0)
    expect(page.locator('[data-section=health] .memo-text').first).to_contain_text('★')
    expect(page.locator('[data-section=longterm] .memo-text').first).to_contain_text('2026年11月2日')
    expect(page.locator('#notes-status [role=status]')).to_have_text('已保存')
    assert page.request.get(endpoint + '/tidy').json()['job']['status'] == 'applied'
    note_menu(daily).get_by_role('button', name='修改', exact=True).click()
    editor.fill('采纳后自动保存的手动修改')
    confirm_action(page, page.locator('#undo-tidy').click, '撤销采纳也会还原', accept=False)
    expect(daily.locator('.memo-text')).to_have_text('采纳后自动保存的手动修改')
    confirm_action(page, page.locator('#undo-tidy').click, '撤销采纳也会还原')
    expect(page.locator('[data-section=health] .memo-row')).to_have_count(2)
    expect(page.locator('#notes-status [role=status]')).to_have_text('已保存')
    assert '采纳后自动保存' not in page.request.get(endpoint).json()['sections']['daily']
    # A simultaneous change blocks adoption and never overwrites the new text.
    page.get_by_role('button', name='秘书整理', exact=True).click()
    page.locator('#start-tidy').click()
    expect(page.locator('#tidy-panel')).to_contain_text('整理建议', timeout=15000)
    latest = page.request.get(endpoint).json()
    result = page.request.put(endpoint, headers=headers, data=dict(expected_version=latest['version'], sections={'daily':'2026年11月10日 夜里新进的合成书签。'}))
    assert result.status == 200
    page.locator('#adopt-tidy').click()
    expect(page.locator('#memos-error')).to_contain_text('重新整理')
    expect(page.locator('#adoption-notice')).to_be_hidden()
    assert '夜里新进' in page.request.get(endpoint).json()['sections']['daily']
    reload_notes = page.locator('#reload-notes')
    expect(reload_notes).to_be_visible()
    reload_notes.click()
    expect(page.locator('[data-section=daily] .memo-text')).to_contain_text('夜里新进')
    expect(reload_notes).to_be_hidden()
    page.locator('#close-tidy').click()
    latest = page.request.get(endpoint).json()
    response = page.request.put(endpoint, headers=headers, data=dict(expected_version=latest['version'], sections={'daily': '另一个页面保存的书签。'}))
    assert response.status == 200, response.text()
    note_menu(daily).get_by_role('button', name='修改', exact=True).click()
    editor.fill('遇到版本冲突的书签。')
    header.locator('.meta').click()
    expect(editor).to_have_count(0)
    expect(status).to_contain_text('保存失败')
    expect(daily.locator('#reload-notes')).to_be_visible()
    confirm_action(page, reload_notes.click, '重新读取会丢弃', accept=False)
    expect(daily.locator('.memo-text')).to_have_text('遇到版本冲突的书签。')
    confirm_action(page, reload_notes.click, '重新读取会丢弃')
    expect(page.locator('[data-section=daily] .memo-text')).to_have_text('另一个页面保存的书签。')
    expect(reload_notes).to_be_hidden()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def main():
    for _ in range(50):
        try:
            if urllib.request.urlopen(ORIGIN + "/login", timeout=1).status == 200:
                break
        except OSError:
            time.sleep(0.2)
    captured = 0
    with sync_playwright() as playwright:
        browser = playwright.webkit.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=1,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
        )
        # The memory explanation pops once per browser; these flows test other pages.
        context.add_init_script("try{localStorage.setItem('amber-guide-seen','1')}catch(e){}")
        context.add_init_script(
            """document.addEventListener('DOMContentLoaded',()=>{
            document.documentElement.style.setProperty('--safe-top','59px');
            document.documentElement.style.setProperty('--safe-bottom','34px');
        });"""
        )

        def guard(route):
            assert route.request.url.startswith(ORIGIN + "/")
            route.continue_()

        context.route("**/*", guard)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def capture(name, target=None):
            nonlocal captured
            page.locator("#cards-result .loading").wait_for(state="detached")
            page.locator("#notice").evaluate("element=>element.textContent=''")
            data = target.screenshot() if target is not None else page.screenshot(full_page=True)
            print(
                "SCREENSHOT:"
                + json.dumps(dict(name=name, data=base64.b64encode(data).decode())),
                flush=True,
            )
            captured += 1

        def layout():
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert page.evaluate(
                "parseFloat(getComputedStyle(document.body).paddingTop)>=59"
            )
            if page.locator(".topbar").count():
                assert (
                    page.locator(".topbar").bounding_box()["y"]
                    + page.evaluate("scrollY")
                    >= 59
                )
                expect(page.locator(".brand-name")).to_have_text("Amber Heart")
                assert page.locator(".brand small").count() == 0
                if page.locator('.shell[data-current-page="overview"]').count() == 0:
                    expect(page.locator(".pagehead h1")).to_be_visible()
                    expect(page.locator(".eyebrow")).to_be_visible()
                assert page.locator("h1").count() == 1
                assert page.locator(".pagehead select, .pagehead #logout").count() == 0
                assert page.evaluate("getComputedStyle(document.documentElement).backgroundColor") == "rgb(246, 243, 238)"
                if page.evaluate("document.documentElement.classList.contains('typing')"):
                    # While a text field has focus the keyboard replaces the bottom nav.
                    expect(page.locator(".topbar nav")).to_be_hidden()
                else:
                    nav = page.locator(".topbar nav").bounding_box()
                    assert nav["y"] + nav["height"] <= page.viewport_size["height"] + 1
                    assert nav["height"] >= 90  # 56px navigation + 34px safe area
                nav_links = page.locator(".topbar nav a")
                assert nav_links.count() == 5
                assert page.locator(".topbar nav a svg").count() == nav_links.count()
                expect(nav_links).to_have_text(["概览", "待审核", "记忆", "记录", "设置"])

        page.goto(ORIGIN + "/login")
        layout()
        capture("login-iphone-webkit.png")
        page.get_by_label("管理员密码", exact=True).fill(PASSWORD)
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_url("**/overview")
        page.locator(".hero").wait_for()
        expect(page.locator(".topbar nav [data-page=review]")).to_be_visible()
        expect(page.locator(".topbar nav a")).to_have_count(5)
        layout()
        expect(page.locator(".headline")).to_contain_text("天")
        expect(page.locator('[data-page="review"] .badge')).to_have_attribute("aria-label", "1 张待审核")
        assert page.locator(".barrow").count() >= 1
        assert page.locator(".callout").bounding_box()["height"] <= 50
        expect(page.locator(".hero .stats > span")).to_have_count(3)
        entries = page.locator(".overview-entries .overview-entry")
        expect(entries).to_have_count(4)
        # The four entry tiles fit the first screen; topic distribution comes last.
        last_entry = entries.nth(3).bounding_box()
        nav = page.locator(".topbar nav").bounding_box()
        assert last_entry["y"] + last_entry["height"] <= nav["y"], (last_entry, nav)
        assert page.locator("#topic-panel").bounding_box()["y"] > last_entry["y"]
        assert entries.nth(0).bounding_box()["y"] == entries.nth(1).bounding_box()["y"]
        assert entries.nth(2).bounding_box()["y"] == entries.nth(3).bounding_box()["y"]
        assert entries.nth(2).bounding_box()["y"] > entries.nth(0).bounding_box()["y"]
        for key, status in (("diaries", "最新一篇"), ("summaries", "最新一份"), ("core-memory", "字"), ("bookmarks", "今天新记")):
            expect(page.locator(f'.overview-entry[data-go="{key}"] .meta')).to_contain_text(status)
        expect(page.locator('.archive-guard')).to_contain_text('自动归档已暂停')
        expect(page.locator('.archive-guard')).to_contain_text('至少需要 20 轮')
        # A new space without an access key gets the "开始使用" card; dismissing it sticks.
        onboarding = page.locator(".onboarding")
        expect(onboarding).to_be_visible()
        expect(onboarding.locator("li").nth(0)).to_have_class("done")
        expect(onboarding.get_by_role("link", name="去设置", exact=True)).to_be_visible()
        expect(onboarding).to_contain_text("导入以前的记忆")
        capture("overview-iphone-webkit.png")
        onboarding.get_by_role("button", name="我知道了", exact=True).click()
        expect(onboarding).to_have_count(0)
        navigation_cache(page)
        page.get_by_role("link", name="设置", exact=True).click()
        page.get_by_role("link", name="概览", exact=True).click()
        page.locator(".hero").wait_for()
        page.wait_for_timeout(500)
        expect(page.locator(".onboarding")).to_have_count(0)
        page.get_by_role("button", name="去审核 →", exact=True).click()
        page.wait_for_url("**/review")
        expect(page.locator("[name=title]")).to_have_value("周末的植物课程")
        expect(page.locator(".draft-card")).to_have_count(1)
        assert page.get_by_role("button", name="已处理", exact=True).count() == 0
        assert page.locator(".listpane, .listitem, #back-list").count() == 0
        layout()
        capture("review-entry-iphone-webkit.png")
        page.get_by_role("link", name="记忆", exact=True).click()
        page.locator(".cardrow").first.wait_for()
        layout()
        assert page.locator("#filter-panel").is_hidden()
        assert page.get_by_role("button", name="查找", exact=True).count() == 0
        assert page.locator(".cardrow").first.bounding_box()["y"] < 650
        expect(page.locator(".topic-pill.active")).to_contain_text("全部")
        pills = page.locator(".topic-pill:not(.active)")
        assert pills.count() >= 1
        pills.first.click()
        expect(page.locator(".topic-pill.active")).not_to_contain_text("全部")
        page.locator(".cardrow").first.wait_for()
        page.locator(".topic-pill").first.click()
        row = page.locator(".cardrow").first
        assert row.bounding_box()["height"] < 115
        heading = row.locator("h2").bounding_box()
        date = row.locator(".carddate").bounding_box()
        assert abs(heading["y"] - date["y"]) < 15
        capture("cards-iphone-webkit.png")
        page.get_by_role("button", name="筛选", exact=True).click()
        for name, hint in [("since", "选择开始日期"), ("until", "选择结束日期")]:
            control = page.locator(f"[name={name}]")
            box = control.bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= 390
            expect(page.get_by_text(hint, exact=True)).to_be_visible()
        page.locator("[name=until]").fill("2026-10-30")
        page.locator("[name=until]").dispatch_event("change")
        page.get_by_role("button", name="取消到 2026-10-30", exact=True).wait_for()
        capture("card-filters-iphone-webkit.png")
        page.get_by_role("button", name="取消到 2026-10-30", exact=True).click()
        expect(page.locator("[name=until]")).to_have_value("")
        page.locator("[name=kind]").select_option("atomic")
        page.get_by_role("button", name="取消记忆卡片", exact=True).click()
        expect(page.locator("[name=kind]")).to_have_value("all")
        page.get_by_label("关键词", exact=True).fill("zqxvneverexists")
        page.get_by_label("关键词", exact=True).press("Enter")
        page.get_by_text(
            "没有找到卡片，试试调整关键词或筛选条件。", exact=True
        ).wait_for()
        page.get_by_label("关键词", exact=True).fill("")
        page.get_by_label("关键词", exact=True).press("Enter")
        page.locator(".cardrow").first.wait_for()
        unfinished_draft_defaults(page)
        page.get_by_role("link", name="待审核", exact=False).click()
        page.locator(".draft-form").wait_for()
        draft = page.locator(".draft-card")
        draft.locator(".draft-form").wait_for()
        assert draft.locator(".draft-head").evaluate("e => getComputedStyle(e, '::before').width") == "4px"
        assert draft.locator(".draft-head").evaluate("e => getComputedStyle(e, '::before').backgroundColor") == "rgb(193, 118, 125)"
        # The accent stays inside the card header, with equal top/bottom space.
        assert draft.locator(".draft-head").evaluate("""e => {
            const s = getComputedStyle(e, '::before');
            const h = e.getBoundingClientRect().height;
            const line = parseFloat(s.height);
            return line <= h - 15 && Math.abs(parseFloat(s.top) - h/2) < 1;
        }""")
        expect(page.get_by_label("摘要", exact=True)).to_be_visible()
        expect(page.get_by_label("里程碑", exact=True)).to_be_visible()
        assert page.get_by_text("一句话概括", exact=True).count() == 0
        assert page.get_by_text("这是一个重要的里程碑", exact=True).count() == 0
        # Topics are tag chips; the native select inside each chip is invisible but tappable.
        assert draft.locator(".topic-select").evaluate_all("""elements => elements.every(e => {
            const chip = getComputedStyle(e.parentElement);
            return getComputedStyle(e).opacity === '0' && parseFloat(chip.borderTopLeftRadius) >= 15;
        })""")
        # Compact text must still reflow when focus raises its font size for iOS.
        content = draft.locator("[name=content]")
        content.focus()
        assert content.evaluate("e => getComputedStyle(e).fontSize") == "16px"
        assert content.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
        draft.locator("[name=title]").focus()
        assert content.evaluate("e => getComputedStyle(e).fontSize") == "13px"
        assert content.evaluate("e => e.scrollHeight <= e.clientHeight + 1")

        # Actions belong at the end of the card and scroll with the document.
        actions = draft.locator(".actions")
        assert actions.evaluate("e => getComputedStyle(e).position") == "static"
        original_content = content.input_value()
        content.fill((original_content + "\n") * 16)
        content.blur()
        page.evaluate("scrollTo(0,0)")
        props = draft.locator(".draft-props").bounding_box()
        assert actions.bounding_box()["y"] >= props["y"] + props["height"]
        assert actions.bounding_box()["y"] > 844
        page.evaluate("scrollBy(0, 180)")
        first = actions.bounding_box()["y"]
        page.evaluate("scrollBy(0, 100)")
        assert abs(actions.bounding_box()["y"] - first + 100) < 2
        content.fill(original_content)
        content.blur()
        page.evaluate("scrollTo(0,0)")
        # A long title must wrap rather than being clipped inside a one-line input.
        title = draft.locator("[name=title]")
        original_title = title.input_value()
        title.fill("合成验收：一起制定阳台小花园的养护计划，每周记录植物的变化")
        for width in (320, 390):
            page.set_viewport_size({"width": width, "height": 844})
            page.wait_for_timeout(100)
            layout()
            assert title.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
            assert title.bounding_box()["height"] > 50
            approve = page.locator(".approve-draft")
            approve.evaluate("e => e.scrollIntoView({block: 'center'})")
            box = approve.bounding_box()
            nav = page.locator(".topbar nav").bounding_box()  # None while typing hides it
            if nav:
                assert box["y"] + box["height"] <= nav["y"] + 1
            assert approve.evaluate("e => {const r=e.getBoundingClientRect(); return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));}")
        title.fill(original_title)

        # Reviewers judge the card; model-filled structure is not a decision.
        for hidden in (
            "importance",
            "valence",
            "arousal",
            "decay_layer",
            "cognitive_status",
            "source_date",
            "experience_mode",
            "topic_ids",
        ):
            assert draft.locator(f"[name={hidden}]").count() == 0
        assert draft.locator("details").count() == 0
        assert draft.get_by_text("实体", exact=False).count() == 0
        assert draft.get_by_text("证据", exact=False).count() == 0
        assert draft.get_by_role("button", name="读取最新", exact=False).count() == 0
        expect(draft.locator(".draft-head .meta")).not_to_contain_text("#")
        expect(draft.locator("[name=experience_mode]")).to_have_count(0)
        expect(draft.locator("[name=is_milestone]")).to_be_visible()
        expect(draft.locator(".edit-hint")).to_have_text("点击空白处可编辑")
        # Milestone sits on the topic caption row; topics read as tag chips.
        head = draft.locator(".prop-head").bounding_box()
        milestone = draft.locator("label.milestone").bounding_box()
        assert head["y"] - 1 <= milestone["y"] and milestone["y"] + milestone["height"] <= head["y"] + head["height"] + 1, (head, milestone)
        selects = draft.locator(".topic-select")
        expect(selects).to_have_count(1)
        initial = selects.first.input_value()
        for _ in range(2):
            draft.get_by_role("button", name="+ 主题", exact=True).click()
            choice = selects.last.locator(
                'option:not([disabled]):not([value=""])'
            ).first.get_attribute("value")
            selects.last.select_option(choice)
        expect(selects).to_have_count(3)
        assert draft.get_by_role("button", name="+ 主题", exact=True).count() == 0
        chosen = selects.evaluate_all("elements=>elements.map(element=>element.value)")
        assert len(set(chosen)) == 3 and chosen[0] == initial
        names = draft.locator(".topic-pick .topic-name").all_inner_texts()
        labels = selects.evaluate_all("elements=>elements.map(element=>element.selectedOptions[0].textContent)")
        assert names == labels, (names, labels)
        expect(draft.locator(".topic-primary")).to_have_count(1)
        expect(selects.first.locator(f'option[value="{chosen[1]}"]')).to_be_disabled()
        for _ in range(2):
            draft.locator(".remove-topic").last.click()
        expect(selects).to_have_count(1)
        expect(selects.first).to_have_value(initial)
        status = draft.locator(".autosave-status [role=status]")
        expect(status).to_have_text("已保存")
        draft_id = page.locator(".draft-card").get_attribute("data-draft-id")
        before = deepcopy(page.request.get(
            ORIGIN + f"/api/spaces/sample/drafts/{draft_id}"
        ).json()["draft"]["payload"])
        # A second writer advances the version. Reload is offered only after
        # the real 409, and cancelling it must keep the current edit intact.
        endpoint = ORIGIN + f"/api/spaces/sample/drafts/{draft_id}"
        latest = page.request.get(endpoint).json()
        response = page.request.put(endpoint, headers={"Origin": ORIGIN,
            "X-CSRF-Token": page.request.get(ORIGIN + "/api/session").json()["csrf"]},
            data=dict(payload=before, expected_version=latest["draft"]["version"],
                expected_policy_revision=latest["configuration"]["revision"]))
        assert response.ok, response.text()
        title = draft.locator("[name=title]")
        conflicted_title = "尚未保存的冲突编辑"
        attempted_titles = []

        def edit_during_save(route):
            if route.request.method == "PUT":
                attempted_titles.append(route.request.post_data_json["payload"]["title"])
                if len(attempted_titles) == 1:
                    title.fill(conflicted_title)
            route.continue_()

        reload = page.get_by_role("button", name="读取最新版本", exact=True)
        page.route(endpoint, edit_during_save)
        try:
            first_title = "触发冲突的第一次编辑"
            title.fill(first_title)
            expect(status).to_contain_text("保存失败")
            expect(reload).to_be_visible()
            for accept, expected_title in ((False, conflicted_title), (True, before["title"])):
                confirm_action(page, reload.click, "丢弃这张草稿", accept=accept)
                expect(title).to_have_value(expected_title)
            # Typing during the failed request and reading latest never retry the stale write.
            assert attempted_titles == [first_title], attempted_titles
        finally:
            page.unroute(endpoint, edit_during_save)
        title.fill("重新读取后的验收标题")
        expect(status).to_have_text("已保存")
        saved = page.request.get(
            ORIGIN + f"/api/spaces/sample/drafts/{draft_id}"
        ).json()["draft"]["payload"]
        # Hidden structure is carried through the save unchanged.
        hidden_fields = ("importance", "valence", "arousal", "decay_layer",
            "cognitive_status", "source_date", "experience_mode", "entities", "relations", "evidence")
        assert {key: saved[key] for key in hidden_fields} == {key: before[key] for key in hidden_fields}
        assert saved["topic_ids"] == [int(initial)], saved["topic_ids"]
        layout()
        capture("draft-iphone-webkit.png")
        expect(title).to_be_focused()
        title.blur()
        page.get_by_role("link", name="设置", exact=True).click()
        page.locator("#participants-form").wait_for()
        layout()
        assert page.get_by_text("配置修订", exact=False).count() == 0
        access_key_settings(page)
        auto_approval_settings(page, capture)
        capture("settings-iphone-webkit.png")
        assert page.locator("#set-topics, [name=ignored_terms], [name=topic_template]").count() == 0
        # A date revision is internal; reviewers need no extra decision for it.
        page.locator("#together-form [name=acquaintance_date]").fill("2024-02-29")
        expect(page.locator("#together-form [role=status]")).to_have_text("已保存")
        page.get_by_role("link", name="待审核", exact=False).click()
        page.locator(".draft-form").wait_for()
        expect(page.locator(".configuration-warning")).to_have_count(0)
        page.get_by_role("link", name="设置", exact=True).click()
        page.locator("[name=user_name]").fill("小禾同学")
        page.locator("[name=user_aliases]").fill("禾禾\n小禾苗")
        page.get_by_role("button", name="保存参与者", exact=True).click()
        page.get_by_role("status").filter(has_text="参与者已保存").wait_for()
        page.get_by_role("link", name="待审核", exact=False).click()
        page.locator(".draft-form").wait_for()
        warning = page.locator(".configuration-warning")
        expect(warning).to_be_visible()
        expect(warning).to_contain_text("你的名字：小禾 → 小禾同学")
        expect(warning).to_contain_text("你的又名已调整")
        expect(warning).to_contain_text("名字和人物指代")
        expect(page.get_by_text("将按新设置收录", exact=True)).to_be_visible()
        capture("configuration-warning-iphone-webkit.png")
        dropped = [False]

        def lost_response(route):
            if not dropped[0]:
                dropped[0] = True
                response = route.fetch()
                assert response.ok, response.text()
                route.abort("failed")
            else:
                route.continue_()

        context.route("**/approve", lost_response)
        page.get_by_role("button", name="确认收录", exact=True).click()
        expect(page.locator(".reader")).to_contain_text("草稿都审完了")
        actual = page.request.get(endpoint).json()["draft"]
        assert actual["status"] == "approved" and actual["memory_item_id"]
        expect(page.locator(".draft-card")).to_have_count(0)
        expect(page.locator('[data-page="review"] .badge')).to_have_count(0)
        expect(page.locator(".topbar nav [data-page=review]")).to_be_visible()
        expect(page.locator(".topbar nav a")).to_have_count(5)
        page.get_by_role("link", name="概览", exact=True).click()
        # The overview no longer lists recent cards; its first screen stays compact.
        expect(page.locator(".recentrow")).to_have_count(0)
        expect(page.locator(".pagehead")).to_have_css("width", "1px")
        expect(page.locator(".today-panel .panelhead")).to_have_count(0)
        page.get_by_role("link", name="记忆", exact=True).click()
        page.wait_for_url("**/memories")
        page.locator(".cardrow").first.locator("summary").click()
        expect(page.locator(".cardrow[open]")).to_have_count(1)
        opened = page.locator(".cardrow[open] .cardbody")
        opened.locator("p.meta").wait_for()
        expect(opened).not_to_contain_text("实体")
        expect(opened).not_to_contain_text("共同赛博经历")
        expect(opened).not_to_contain_text("现实经历")
        expect(opened).not_to_contain_text("#")
        page.get_by_role("link", name="设置", exact=True).click()
        page.locator("#together-form [name=acquaintance_date]").fill("2026-02-14")
        expect(page.locator("#together-form [role=status]")).to_have_text("已保存")
        page.get_by_role("link", name="概览", exact=True).click()
        expect(page.locator(".hero-intro")).to_contain_text("已经相伴")
        # The day count speaks for itself; no "counted from" explanation underneath.
        expect(page.locator(".hero .muted")).to_have_count(0)
        layout()
        capture("overview-together-iphone-webkit.png")
        settings_unsaved_edits(page)
        hosted_pages(page, capture)
        conversation_scrollback(page)
        conversation_delete(page)
        page.get_by_role("link", name="设置", exact=True).click()
        persona_imports(page)
        diary_pages(page, capture)
        review_queue_list(page, capture)
        memos_page(page, capture, "iphone-webkit", pending_seeded=True)
        settings_space_switch(page)
        card_mutations(page, capture, "iphone-webkit")
        topic_summary_pages(page,capture,"iphone-webkit")
        page.get_by_role("link", name="设置", exact=True).click()
        assert not errors, errors
        page.get_by_role("button", name="退出登录", exact=True).click()
        page.wait_for_url("**/login")
        browser.close()
        # Desktop typography and navigation use a separate engine and clean login.
        browser = playwright.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-crashpad-for-testing"]
        )
        desktop = browser.new_page(viewport={"width": 1366, "height": 900})
        desktop.add_init_script("try{localStorage.setItem('amber-guide-seen','1')}catch(e){}")
        desktop.goto(ORIGIN + "/login")
        desktop.get_by_label("管理员密码", exact=True).fill(PASSWORD)
        desktop.get_by_role("button", name="登录", exact=True).click()
        desktop.wait_for_url("**/overview")
        desktop.locator(".hero .headline").wait_for(state="visible")
        expect(desktop.locator(".brand-name")).to_have_text("Amber Heart")
        assert desktop.locator(".brand small").count() == 0
        data = desktop.screenshot(full_page=True)
        print(
            "SCREENSHOT:"
            + json.dumps(
                dict(
                    name="header-desktop-chromium.png",
                    data=base64.b64encode(data).decode(),
                )
            ),
            flush=True,
        )
        desktop.get_by_role("link", name="记忆", exact=True).click()
        desktop.locator(".cardrow").first.wait_for()
        assert desktop.evaluate("document.documentElement.scrollWidth<=innerWidth")
        open_overview_page(desktop, "日记", "diaries")
        desktop.locator(".diary-summary").first.click()
        expect(desktop.locator(".diary-body")).to_contain_text("浏览器修改后的日记")
        desktop.get_by_role("button", name="编辑", exact=True).click()
        desktop.locator("#diary-content").fill("# 林舟的日记\n桌面保存后的合成日记。")
        expect(desktop.locator(".diary-form [role=status]")).to_have_text("已保存")
        expect(desktop.locator("#diary-content")).to_be_focused()
        desktop.get_by_role("button", name="完成编辑", exact=True).click()
        expect(desktop.locator(".diary-body")).to_contain_text("桌面保存后的合成日记")
        assert desktop.evaluate("document.documentElement.scrollWidth<=innerWidth")
        print(
            "SCREENSHOT:"
            + json.dumps(
                dict(
                    name="diaries-desktop-chromium.png",
                    data=base64.b64encode(desktop.screenshot(full_page=True)).decode(),
                )
            ),
            flush=True,
        )
        def capture_desktop(name, target=None):
            nonlocal captured
            desktop.locator('#notice').evaluate("element=>element.textContent=''")
            print('SCREENSHOT:'+json.dumps(dict(name=name,data=base64.b64encode(target.screenshot() if target is not None else desktop.screenshot(full_page=True)).decode())),flush=True)
            captured += 1
        card_mutations(desktop,capture_desktop,'desktop-chromium')
        topic_summary_pages(desktop,capture_desktop,'desktop-chromium')
        desktop.get_by_role("link", name="设置", exact=True).click()
        open_conversation_records(desktop)
        desktop.locator('.conversation-list').wait_for()
        assert desktop.evaluate('document.documentElement.scrollWidth<=innerWidth')
        desktop.get_by_role('link', name='设置', exact=True).click()
        expect(desktop.locator('#auto-approval')).to_be_checked()
        expect(desktop.locator('.topbar nav a')).to_have_count(5)
        desktop.locator('#auto-approval').uncheck()
        expect(desktop.locator('#auto-approval-form [role=status]')).to_have_text('已保存')
        desktop.reload()
        expect(desktop.locator('#auto-approval')).not_to_be_checked()
        expect(desktop.locator('.topbar nav [data-page=review]')).to_be_visible()
        capture_desktop('auto-approval-desktop-chromium.png')
        desktop.locator('#auto-approval').check()
        expect(desktop.locator('#auto-approval-form [role=status]')).to_have_text('已保存')
        persona_imports(desktop)
        desktop.get_by_role('link',name='概览',exact=True).click()
        expect(desktop.locator('.conversation-entry')).to_have_count(0)
        expect(desktop.locator('.topbar nav a')).to_have_count(5)
        capture_desktop('overview-navigation-desktop-chromium.png')
        memos_page(desktop, capture_desktop, 'desktop-chromium')
        desktop.get_by_role('link',name='设置',exact=True).click()
        desktop.get_by_role('button',name='退出登录',exact=True).click()
        desktop.wait_for_url('**/login')
        assert desktop.request.get(ORIGIN+'/api/session').status==401
        browser.close()
        print(
            json.dumps(
                dict(
                    result="passed",
                    engines=["WebKit", "Chromium"],
                    safe_area=dict(top=59, bottom=34),
                    screenshots=captured + 2,
                ),
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()

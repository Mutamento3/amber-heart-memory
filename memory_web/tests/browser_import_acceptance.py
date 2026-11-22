"""Real browser uploads against the isolated import preview and synthetic models."""

import base64
import json
import time
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ORIGIN='http://localhost:8080'


def capture(page,name):
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),name
    print('SCREENSHOT:'+json.dumps(dict(name=name,data=base64.b64encode(page.screenshot(full_page=True)).decode())),flush=True)


def upload(page,source,name,content):
    page.locator('#import-source').select_option(source)
    page.locator('#import-file').set_input_files(dict(name=name,mimeType='application/octet-stream',buffer=content))
    page.get_by_role('button',name='上传并预览',exact=True).click()
    expect(page.locator('#import-preview')).to_be_visible()


def wait_for_preview():
    url = ORIGIN + '/login'
    deadline = time.monotonic() + 30
    while True:
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
                failure = f'HTTP {response.status}'
        except OSError as error:
            failure = str(error)
        if time.monotonic() >= deadline:
            raise RuntimeError(f'导入预览 30 秒内未就绪（{url}，{failure}）')
        time.sleep(0.2)


def check_remaining_time(page):
    endpoint = ORIGIN + '/api/spaces/sample/imports'
    job = dict(id='synthetic-estimate', source='memory', status='running', model_calls=0,
        created_at='2026-04-01T12:00:00+08:00',
        progress=dict(total=5, processed=0, failed=0, skipped=0, running=2,
            estimated_remaining_seconds=None))

    def serve_progress(route):
        response = route.fetch()
        body = response.json()
        body['jobs'].append(job)
        route.fulfill(response=response, json=body)

    refresh = page.get_by_role('button', name='刷新', exact=True)
    card = page.locator('[data-import-job="synthetic-estimate"]')
    estimate = card.locator('.import-estimate')
    page.route(endpoint, serve_progress)
    try:
        refresh.click()
        expect(estimate).to_have_text('正在估算…')
        job['progress'].update(processed=1, estimated_remaining_seconds=245)
        refresh.click()
        expect(estimate).to_have_text('预计还需约 4 分钟')
        job['progress'].update(processed=4, estimated_remaining_seconds=45)
        refresh.click()
        expect(estimate).to_have_text('预计还需不到 1 分钟')
        for status, label in (('failed', '已暂停'), ('queued', '排队中'), ('completed', '已完成')):
            job['status'] = status
            refresh.click()
            expect(card.locator('.import-job-head')).to_contain_text(label)
            expect(estimate).to_have_count(0)
    finally:
        page.unroute(endpoint, serve_progress)
    refresh.click()
    expect(card).to_have_count(0)


def main():
    wait_for_preview()
    with sync_playwright() as playwright:
        browser=playwright.webkit.launch()
        page=browser.new_page(viewport=dict(width=375,height=812),is_mobile=True,has_touch=True)
        page.add_init_script("try{localStorage.setItem('amber-guide-seen','1')}catch(e){}")
        errors=[]
        page.on('pageerror',lambda error: errors.append(str(error)))
        page.goto(ORIGIN+'/login')
        page.get_by_label('管理员密码',exact=True).fill('synthetic acceptance password')
        page.get_by_role('button',name='登录',exact=True).click()
        page.wait_for_url('**/overview')
        expect(page.locator('.hero')).to_be_visible()
        expect(page.locator('.conversation-entry')).to_have_count(0)
        expect(page.locator('.topbar nav a')).to_have_text(['概览', '待审核', '记忆', '召回', '设置'])
        expect(page.locator('.overview-entries strong')).to_have_text(['日记', '及时摘要', '核心记忆', '记忆书签'])
        expect(page.locator('.overview-entry[data-go="diaries"] .meta')).to_contain_text('最新一篇')
        expect(page.locator('.overview-entry[data-go="summaries"] .meta')).to_contain_text('最新一份')
        # Import is a one-off setup step: it lives in settings, not on the daily overview.
        expect(page.locator('main [data-go=imports]')).to_have_count(0)
        page.get_by_role('link',name='设置',exact=True).click()
        page.get_by_role('button',name='去导入',exact=True).click()
        expect(page.get_by_role('button',name='选择文件',exact=True)).to_be_visible()
        expect(page.locator('.topbar nav [data-page=settings]')).to_have_attribute('aria-current','page')
        expect(page.locator('.topbar nav a')).to_have_count(5)
        expect(page.locator('.topbar nav [data-page=imports]')).to_have_count(0)
        check_remaining_time(page)
        records=[]
        for key in ('a','b'):
            records.append(dict(uuid='synthetic-browser-'+key,name='合成对话 '+key,chat_messages=[
                dict(sender='human',text='合成原话：周末一起学习陶艺。',created_at='2025-03-14T10:00:00Z'),
                dict(sender='assistant',text='合成回应：一起做笔记。',created_at='2025-03-14T10:01:00Z')]))
        content=json.dumps(records,ensure_ascii=False).encode()
        upload(page,'claude','conversations.json',content)
        choices=page.locator('[data-import-conversation]')
        expect(choices).to_have_count(2)
        expect(choices.nth(0)).to_be_checked()
        expect(choices.nth(1)).to_be_checked()
        choices.nth(1).uncheck()
        expect(page.locator('#import-counts')).to_contain_text('2 条消息')
        approve_directly=page.get_by_role('checkbox',name='整理好后直接收录，不进待审核')
        expect(approve_directly).to_be_checked()
        approve_directly.uncheck()
        capture(page,'import-conversations-mobile.png')
        page.get_by_role('button',name='确认导入',exact=True).click()
        expect(page.locator('#import-jobs')).to_contain_text('已完成',timeout=12000)
        expect(page.locator('#import-jobs')).to_contain_text('已调用模型 2 次')
        page.reload()
        expect(page.locator('#import-jobs')).to_contain_text('已完成')

        upload(page,'claude','conversations.json',content)
        for choice in page.locator('[data-import-conversation]').all():
            if '已导过' not in choice.locator('..').inner_text():
                choice.uncheck()
        expect(page.locator('#import-skip')).to_contain_text('已经导过')
        expect(page.get_by_role('button',name='确认导入',exact=True)).to_be_disabled()
        page.get_by_role('button',name='收起预览',exact=True).click()

        activity=[dict(header='Gemini Apps',title='Prompted 合成：今天开始学陶艺。',time='2025-03-14T19:00:00Z',
            subtitles=[dict(name='Attachment',value='SYNTHETIC_ATTACHMENT.pdf')],safeHtmlItem=[dict(html='<p>合成回答：下次一起做杯子。</p>')]),
            dict(header='Gemini Apps',title='Prompted 合成：我喜欢看兰花。',time='2025-03-14T18:59:59Z',safeHtmlItem=[dict(html='<p>合成回答：一起去植物园。</p>')])]
        upload(page,'gemini','MyActivity.json',json.dumps(activity,ensure_ascii=False).encode())
        expect(page.locator('[data-import-conversation]')).to_have_count(0)
        first=page.locator('#import-start').input_value()
        page.locator('#import-end').fill(first)
        page.locator('#import-end').dispatch_event('change')
        expect(page.locator('#import-counts')).to_contain_text('1 天')
        page.set_viewport_size(dict(width=320,height=740))
        capture(page,'import-gemini-320px.png')
        approve_directly.uncheck()
        page.get_by_role('button',name='确认导入',exact=True).click()
        gemini_job = page.locator('.import-job').filter(has=page.get_by_text('Gemini', exact=True))
        expect(gemini_job.locator('.import-job-head')).to_contain_text('已完成', timeout=12000)

        data='## 2025-03-14\n合成记忆：每周练习陶艺。\n\n## 2025-03-15\n合成记忆：第一次做好的杯子。'.encode()
        upload(page,'memory','memories.md',data)
        expect(page.locator('#import-sample')).to_be_visible()
        expect(page.locator('#import-counts')).to_contain_text('2 条记忆')
        expect(page.locator('#import-counts')).to_contain_text('2 次模型调用')
        expect(approve_directly).to_be_checked()
        page.set_viewport_size(dict(width=375,height=812))
        capture(page,'import-memory-mobile.png')
        page.get_by_role('button',name='确认导入',exact=True).click()
        memory_job = page.locator('.import-job').filter(has=page.get_by_text('自己整理的记忆', exact=True))
        expect(memory_job.locator('.import-job-head')).to_contain_text('已完成', timeout=12000)
        expect(memory_job).to_contain_text('已调用模型 2 次')
        cards=page.request.get(ORIGIN+'/api/spaces/sample/cards?size=100').json()['items']
        imported=next(card for card in cards if card['title']=='合成记忆标题')
        detail=page.request.get(ORIGIN+f'/api/spaces/sample/cards/{imported["id"]}').json()
        assert detail['import_original']['text'] in ('合成记忆：每周练习陶艺。','合成记忆：第一次做好的杯子。')
        capture(page,'import-progress-mobile.png')

        data='\n'.join(f'## 2025-04-{n:02d}\n合成浏览器在途材料 {n}。\n' for n in range(1,5)).encode()
        upload(page,'memory','stop.md',data)
        job=memory_job.first
        page.get_by_role('button',name='确认导入',exact=True).click()
        job.get_by_role('button',name='停止',exact=True).click()
        expect(job).to_contain_text('已手动停止，可以随时继续')
        expect(job).to_contain_text('失败 0 条')
        expect(job.locator('.import-estimate')).to_have_count(0)
        job.get_by_role('button',name='继续',exact=True).click()
        expect(job.locator('.import-job-head')).to_contain_text('已完成',timeout=15000)
        expect(job.locator('.import-estimate')).to_have_count(0)
        expect(job).to_contain_text('已调用模型 4 次')

        data='## 2025-05-01\n合成浏览器待重试材料。\n\n## 2025-05-02\n合成浏览器正常材料。'.encode()
        upload(page,'memory','retry.md',data)
        page.get_by_role('button',name='确认导入',exact=True).click()
        expect(job.locator('.import-job-head')).to_contain_text('完成，1 条失败',timeout=12000)
        expect(job).to_contain_text('已处理 2 / 2 份原材料 · 失败 1 条')
        expect(job).to_contain_text('已调用模型 3 次')
        capture(page,'import-partial-failure-mobile.png')
        job.get_by_role('button',name='重试失败的 1 条',exact=True).click()
        expect(job.locator('.import-job-head')).to_contain_text('已完成',timeout=12000)
        expect(job).to_contain_text('已调用模型 4 次')
        page.get_by_role('link',name='记忆',exact=True).click()
        card=page.locator(f'.cardrow[data-card="{imported["id"]}"]')
        card.locator(':scope > summary').click()
        card.get_by_text('导入原文',exact=True).click()
        expect(card.locator('.import-original .card-content')).to_have_text(detail['import_original']['text'])
        assert not errors,errors
        browser.close()

        browser=playwright.chromium.launch()
        desktop=browser.new_page(viewport=dict(width=1280,height=900))
        desktop.add_init_script("try{localStorage.setItem('amber-guide-seen','1')}catch(e){}")
        desktop.goto(ORIGIN+'/login')
        desktop.get_by_label('管理员密码',exact=True).fill('synthetic acceptance password')
        desktop.get_by_role('button',name='登录',exact=True).click()
        desktop.wait_for_url('**/overview')
        desktop.get_by_role('link',name='设置',exact=True).click()
        desktop.get_by_role('button',name='去导入',exact=True).click()
        expect(desktop.locator('.import-job')).to_have_count(6)
        capture(desktop,'import-progress-desktop.png')
        browser.close()
        print(json.dumps(dict(result='passed',engines=['WebKit','Chromium'],screenshots=6)),flush=True)


if __name__=='__main__':
    main()

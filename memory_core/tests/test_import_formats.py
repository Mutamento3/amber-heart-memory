"""Synthetic official export shapes only; no files from user exports or services."""

from datetime import datetime
from io import BytesIO
import json
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from memory_core.storage.config import Settings
from memory_core.cards.extraction_contract import ContractError, check_credentials, material_input
from memory_core.imports.import_formats import ImportFormatError, parse_upload
from memory_core.imports.import_preview import preview


SETTINGS = Settings(3)


def uploaded(value, filename="conversations.json"):
    return [(filename, json.dumps(value, ensure_ascii=False).encode())]


def packed(files):
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for filename, data in files:
            archive.writestr(filename, data)
    return [("official-export.zip", buffer.getvalue())]


def node(role, body, *, parent=None, stamp="2025-03-14T02:59:59+08:00", **extra):
    return dict(parent=parent, children=[], message=dict(
        author=dict(role=role), create_time=datetime.fromisoformat(stamp).timestamp(),
        content=dict(content_type="text", parts=[body]), **extra))


def chatgpt():
    return dict(id="synthetic-conversation", title="合成：春日散步", current_node="chosen",
        mapping=dict(root=dict(parent=None, message=None),
            question=node("user", "今天去了植物园。", parent="root"),
            abandoned=node("assistant", "未采用的回复，不能被导入。", parent="question"),
            chosen=node("assistant", "记住了，你很喜欢那里的兰花。", parent="question",
                stamp="2025-03-14T03:00:00+08:00")))


def claude(*, modern=False, key="synthetic-claude", stamp="2025-03-14T10:00:00Z"):
    messages = [dict(created_at=stamp, **({"role":"user", "content":[
        dict(type="text", text="周末开始学陶艺。"), dict(type="image", source={})]}
        if modern else {"sender":"human", "text":"周末开始学陶艺。"})),
        dict(created_at=stamp, **({"role":"assistant", "content":[
            dict(type="thinking", thinking="合成思考不导入"), dict(type="text", text="下次听你分享。") ]}
            if modern else {"sender":"assistant", "text":"下次听你分享。"}))]
    return dict(uuid=key, name="合成：陶艺", **{"messages" if modern else "chat_messages":messages})


def gemini():
    return [dict(header="Gemini Apps", title="Prompted 明天一起去看海。",
        time="2025-03-14T19:00:00Z", safeHtmlItem=[dict(html="<p>好呀 &amp; 记得带水。</p><p>一起看日落。<script>合成脚本</script></p>")]),
        dict(header="Gemini Apps", title="Prompted with: 今天的陶杯做好了。",
            time="2025-03-14T18:59:59Z", subtitles=[dict(name="You",value="今天的陶杯做好了。")],
            safeHtmlItem=[dict(html="<div>真好！<br>等你分享。</div>")]),
        dict(header="Search", title="Searched for 合成搜索记录，不可导入",time="2025-03-14T18:00:00Z")]


class OfficialFormatTests(unittest.TestCase):
    def test_chatgpt_follows_only_chosen_branch_and_exact_business_boundary(self):
        items = parse_upload("chatgpt", uploaded([chatgpt()]), SETTINGS)
        self.assertEqual([item.business_date for item in items], ["2025-03-13", "2025-03-14"])
        self.assertEqual([segment["text"] for item in items for segment in item.segments],
            ["今天去了植物园。", "记住了，你很喜欢那里的兰花。"])
        self.assertTrue(all(segment["timestamp"].endswith("+08:00") for item in items for segment in item.segments))

    def test_chatgpt_ignores_hidden_thinking_tools_system_and_nontext(self):
        data = chatgpt()
        data["mapping"] = dict(root=node("system", "合成系统指令"),
            hidden=node("user", "合成隐藏消息", parent="root", metadata=dict(is_visually_hidden_from_conversation=True)),
            thought=node("assistant", "合成思考", parent="hidden", channel="analysis"),
            tool=node("assistant", "合成工具参数", parent="thought", recipient="browser"),
            image=node("user", "", parent="tool"),
            chosen=node("assistant", "保留的文字。", parent="image"))
        data["mapping"]["image"]["message"]["content"] = dict(content_type="multimodal_text",parts=[dict(content_type="image_asset_pointer")])
        items=parse_upload("chatgpt",uploaded([data]),SETTINGS)
        self.assertEqual([s["text"] for s in items[0].segments],["保留的文字。"])

    def test_chatgpt_sharded_zip_ignores_attachments_and_deduplicates_repeated_shard(self):
        first, second = chatgpt(), chatgpt()
        second["id"] = "synthetic-other"
        files = uploaded([first],"export/conversations-000.json") + uploaded([second],"export/conversations-001.json")
        files += uploaded([first],"export/conversations.json")
        files.append(("export/private-attachment.txt",b"Ignored synthetic attachment"))
        items=parse_upload("chatgpt",packed(files),SETTINGS)
        self.assertEqual(len(items),4)
        self.assertEqual(len({item.source_key for item in items}),4)

    def test_broken_chatgpt_branch_is_rejected_without_echoing_contents(self):
        for failure in ("missing", "cycle", "broken-parent"):
            with self.subTest(failure=failure):
                data=chatgpt()
                if failure=="missing": data["current_node"]="missing"
                elif failure=="cycle": data["mapping"]["question"]["parent"]="chosen"
                else: data["mapping"]["question"]["parent"]="missing"
                with self.assertRaises(ImportFormatError) as error:
                    parse_upload("chatgpt",uploaded([data]),SETTINGS)
                self.assertNotIn("植物园",str(error.exception))

    def test_claude_both_official_key_sets_and_text_only_blocks(self):
        old=parse_upload("claude",uploaded([claude()]),SETTINGS)
        new=parse_upload("claude",uploaded([claude(modern=True)]),SETTINGS)
        self.assertEqual(old,new)
        self.assertEqual([s["speaker"] for s in new[0].segments],["user","assistant"])
        self.assertEqual(new[0].business_date,"2025-03-14")

    def test_claude_zip_and_bom_are_accepted(self):
        data=uploaded([claude()])[0][1]
        items=parse_upload("claude",packed([("conversations.json",b"\xef\xbb\xbf"+data)]),SETTINGS)
        self.assertEqual(len(items[0].segments),2)

    def test_claude_empty_content_blocks_preserve_legacy_text_and_title(self):
        record=claude()
        record['title']=record.pop('name')
        for message in record['chat_messages']:
            message['content']=[]
        items=parse_upload('claude',uploaded([record]),SETTINGS)
        self.assertEqual(items[0].title,'合成：陶艺')
        self.assertEqual(len(items[0].segments),2)

    def test_gemini_is_daily_activity_not_invented_conversations(self):
        items=parse_upload("gemini",uploaded(gemini(),"MyActivity.json"),SETTINGS)
        self.assertEqual([item.business_date for item in items],["2025-03-14","2025-03-15"])
        self.assertEqual(len({item.conversation_key for item in items}),1)
        self.assertEqual(items[0].segments[0]["text"],"今天的陶杯做好了。")
        self.assertEqual(items[1].segments[0]["text"],"明天一起去看海。")
        answer=items[1].segments[1]["text"]
        self.assertEqual(answer,"好呀 & 记得带水。\n\n一起看日落。")
        self.assertNotIn("合成脚本",answer)

    def test_gemini_takeout_zip_only_reads_gemini_activity(self):
        files=uploaded(gemini(),"Takeout/我的活動/應用程式聊天記錄/MyActivity.json")
        files+=uploaded([dict(header="Search")],"Takeout/My Activity/Search/MyActivity.json")
        items=parse_upload("gemini",packed(files),SETTINGS)
        self.assertEqual(sum(len(item.segments) for item in items),4)

    def test_gemini_localized_title_with_answer_is_preserved(self):
        record=gemini()[0]
        record['title']='本地化的合成提问前缀：明天一起去看海。'
        items=parse_upload('gemini',uploaded([record]),SETTINGS)
        self.assertEqual(items[0].segments[0]['text'],record['title'])
        self.assertEqual(len(items[0].segments),2)

    def test_gemini_attachment_and_gem_metadata_never_become_source_text(self):
        record=gemini()[0]
        record['subtitles']=[dict(name='Attachment',value='SYNTHETIC_ATTACHMENT_NAME.pdf'),
            dict(name='Synthetic Gem was used in this chat. Manage your Gems.',value='SYNTHETIC_GEM_NAME')]
        question_only=dict(header='Gemini Apps',title='Prompted 合成的无回答提问。',time='2025-03-14T19:01:00Z')
        metadata=[dict(header='Gemini Apps',title=title,time='2025-03-14T20:00:00Z')
            for title in ('Gave feedback','Created','Cleared','Selected')]
        items=parse_upload('gemini',uploaded([record,question_only,*metadata]),SETTINGS)
        rendered='\n'.join(segment['text'] for item in items for segment in item.segments)
        self.assertNotIn('SYNTHETIC_ATTACHMENT_NAME',rendered)
        self.assertNotIn('SYNTHETIC_GEM_NAME',rendered)
        self.assertEqual(sum(len(item.segments) for item in items),3)

    def test_gemini_html_and_other_product_only_exports_are_rejected(self):
        for files in ([('MyActivity.html',b'<html>synthetic</html>')],
                uploaded([dict(header="Search",title="Searched",time="2025-01-01T00:00:00Z")])):
            with self.assertRaises(ImportFormatError):
                parse_upload("gemini",files,SETTINGS)

    def test_installation_timezone_and_dst_are_used(self):
        settings=Settings(3,"America/New_York",3)
        record=claude(stamp="2025-03-09T06:59:59Z")
        other=claude(key="synthetic-after-dst",stamp="2025-03-09T07:00:00Z")
        items=parse_upload("claude",uploaded([record,other]),settings)
        self.assertEqual([item.business_date for item in items],["2025-03-08","2025-03-09"])
        self.assertTrue(items[0].segments[0]["timestamp"].endswith("-05:00"))
        self.assertTrue(items[1].segments[0]["timestamp"].endswith("-04:00"))

    def test_missing_or_naive_timestamps_and_ids_do_not_guess_upload_date(self):
        for value in (None,True,float('inf'),"2025-03-14T10:00:00"):
            data=claude(stamp=value)
            with self.assertRaises(ImportFormatError):
                parse_upload("claude",uploaded([data]),SETTINGS)
        data=claude()
        data.pop('uuid')
        with self.assertRaises(ImportFormatError):
            parse_upload("claude",uploaded([data]),SETTINGS)

    def test_conflicting_uploads_cannot_silently_choose_one(self):
        first,second=claude(),claude()
        second['chat_messages'][0]['text']='另一份合成内容'
        with self.assertRaisesRegex(ImportFormatError,'不同原文'):
            parse_upload('claude',uploaded([first,second]),SETTINGS)

    def test_size_cap_also_applies_after_zip_expansion(self):
        data=claude()
        data['chat_messages'][0]['text']='合成的重复文字。' * 1000
        expanded=uploaded([data])
        files=packed(expanded)
        self.assertLess(len(files[0][1]),len(expanded[0][1]))
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            parse_upload('claude',files,SETTINGS,max_json_bytes=len(files[0][1])+1)
        with self.assertRaisesRegex(ImportFormatError,'上限'):
            parse_upload('claude',uploaded([claude()]),SETTINGS,max_bytes=20)

    def test_zip_paths_are_not_extracted_or_accepted(self):
        for filename in ('../conversations.json','/conversations.json'):
            with self.assertRaisesRegex(ImportFormatError,'路径'):
                parse_upload('claude',packed(uploaded([claude()],filename)),SETTINGS)

    def test_invalid_zip_encoding_and_json_fail_with_safe_errors(self):
        for source,files in [('claude',[('export.zip',b'not zip')]),
                ('claude',[('conversations.json',b'\xff')]),
                ('claude',[('conversations.json',b'{"private_synthetic":"invalid"}')])]:
            with self.assertRaises(ImportFormatError) as error:
                parse_upload(source,files,SETTINGS)
            self.assertNotIn('private_synthetic',str(error.exception))

    def test_parsed_material_uses_the_existing_draft_input_contract(self):
        cases=[('chatgpt',uploaded([chatgpt()])),('claude',uploaded([claude()])),
            ('gemini',uploaded(gemini())),('memory',[('memory.md','## 2025-03-14\n合成记忆。'.encode())])]
        for source,files in cases:
            with self.subTest(source=source):
                for item in parse_upload(source,files,SETTINGS):
                    segments,day=material_input(item.segments,SETTINGS,item.business_date)
                    self.assertEqual((segments,day),(item.segments,item.business_date))
                    check_credentials(item.source_key)


class MemoryFileTests(unittest.TestCase):
    def test_memory_dates_are_content_dates_and_body_is_preserved(self):
        body='## 2025-03-14\r\n想要每周学一次陶艺。\r\n\r\n这是自己的约定。\r\n## 2025-03-15\r\n第一次做好杯子。\r\n'
        items=parse_upload('memory',[('memory.md',body.encode())],SETTINGS)
        self.assertEqual([item.business_date for item in items],['2025-03-14','2025-03-15'])
        self.assertEqual(items[0].segments[0]['text'],'想要每周学一次陶艺。\n\n这是自己的约定。')
        self.assertIsNone(items[0].segments[0]['timestamp'])
        custom=parse_upload('memory',[('memory.txt',body.encode())],Settings(3,'America/New_York',23))
        self.assertEqual(items,custom)

    def test_memory_dedup_uses_both_date_and_content(self):
        body='## 2025-03-14\n第一条合成记忆。\n## 2025-03-14\n第二条合成记忆。\n## 2025-03-15\n第一条合成记忆。'
        items=parse_upload('memory',[('memory.md',body.encode()),('again.txt',body.encode())],SETTINGS)
        self.assertEqual(len(items),3)
        self.assertEqual(len({item.source_key for item in items}),3)

    def test_memory_code_fence_does_not_create_a_phantom_date(self):
        body='## 2025-03-14\n保留一段代码：\n```markdown\n## 2020-01-01\n这是代码内容\n```'
        items=parse_upload('memory',[('memory.md',body.encode())],SETTINGS)
        self.assertEqual(len(items),1)
        self.assertIn('## 2020-01-01',items[0].segments[0]['text'])

    def test_memory_dates_may_be_a_month_a_year_or_not_given(self):
        body='## 2025-03\n月份记忆。\n## 2025\n年份记忆。\n## 未注明\n无日期记忆。\n## 小标题仍是正文\n'
        items=parse_upload('memory',[('memory.md',body.encode())],SETTINGS)
        self.assertEqual({item.segments[0]['text']:item.business_date for item in items},
            {'月份记忆。':'2025-03','年份记忆。':'2025','无日期记忆。\n## 小标题仍是正文':None})
        undated=next(item for item in items if item.business_date is None)
        self.assertEqual((undated.title,undated.source_key[:21]),('未注明','import:memory:undated'))
        for item in items:
            segments,day=material_input(item.segments,SETTINGS,item.business_date,
                allow_undated=item.business_date is None,partial_date=True)
            self.assertEqual(day,item.business_date)
        with self.assertRaises(ContractError):
            material_input(items[0].segments,SETTINGS,'2025-03')
        summary=preview(items,daily_limit=5)
        self.assertEqual((summary['memory_count'],summary['first_date'],summary['last_date']),(3,'2025','2025-03'))

    def test_invalid_empty_and_unlabelled_memory_are_rejected(self):
        for body in ('随手写的无日期内容','## 2025-02-30\n无效日期','## 2025-03-14\n','## 2025-13\n无效月份','## 2025-3\n月份要两位',''):
            with self.assertRaises(ImportFormatError):
                parse_upload('memory',[('memory.md',body.encode())],SETTINGS)


class PreviewTests(unittest.TestCase):
    def test_conversations_default_to_all_and_deselection_changes_workload(self):
        items=parse_upload('claude',uploaded([claude(),claude(key='synthetic-second')]),SETTINGS)
        all_selected=preview(items,daily_limit=5)
        self.assertTrue(all(row['selected'] for row in all_selected['conversations']))
        self.assertEqual((all_selected['day_count'],all_selected['message_count']), (1,4))
        # Both conversations share five available card slots, not ten.
        self.assertEqual((all_selected['model_calls_min'],all_selected['model_calls_max']),(2,7))
        one=preview(items,daily_limit=5,used_per_day={'2025-03-14':4},
            selected_conversations=[items[0].conversation_key])
        self.assertEqual((one['message_count'],one['model_calls_min'],one['model_calls_max']),(2,1,2))
        empty=preview(items,daily_limit=5,selected_conversations=[])
        self.assertEqual((empty['message_count'],empty['model_calls_max']),(0,0))

    def test_imported_days_are_skipped_from_counts_and_model_calls(self):
        items=parse_upload('chatgpt',uploaded([chatgpt()]),SETTINGS)
        result=preview(items,daily_limit=5,imported_keys=[items[0].source_key])
        self.assertEqual((result['day_count'],result['message_count'],result['skipped_count']),(1,1,1))
        self.assertEqual(result['source_keys'],[items[1].source_key])

    def test_gemini_date_range_is_inclusive_and_not_a_conversation_selector(self):
        items=parse_upload('gemini',uploaded(gemini()),SETTINGS)
        result=preview(items,daily_limit=5,date_start='2025-03-14',date_end='2025-03-14')
        self.assertEqual(result['conversations'],[])
        self.assertEqual((result['day_count'],result['message_count']),(1,2))
        for kwargs in ({'date_start':'2025-03-16','date_end':'2025-03-14'},
                {'date_start':'2025-02-30'},{'selected_conversations':[]}):
            with self.assertRaises(ImportFormatError):
                preview(items,daily_limit=5,**kwargs)

    def test_memory_skips_selection_and_does_not_use_chat_daily_quota(self):
        items=parse_upload('memory',[('memory.md','## 2025-03-14\n合成记忆一\n## 2025-03-14\n合成记忆二'.encode())],SETTINGS)
        result=preview(items,daily_limit=1,used_per_day={'2025-03-14':10})
        self.assertEqual((result['day_count'],result['memory_count'],result['message_count']),(1,2,0))
        self.assertEqual((result['model_calls_min'],result['model_calls_max']),(2,2))

    def test_invalid_selection_is_not_silently_ignored(self):
        items=parse_upload('claude',uploaded([claude()]),SETTINGS)
        for kwargs in ({'selected_conversations':['unknown']},{'selected_conversations':'all'},
                {'date_start':'2025-03-14'}):
            with self.assertRaises(ImportFormatError):
                preview(items,daily_limit=5,**kwargs)


if __name__ == '__main__':
    unittest.main()

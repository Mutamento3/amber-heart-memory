"""Lock the accepted chat prompt byte for byte while sharing structure rules."""

import hashlib
import json
from pathlib import Path
import unittest

from memory_core.cards.extraction_contract import render_prompt, structure_template

PREVIOUS_PARAGRAPH='不同经历层的独立事件确有必要时分卡，不输出 mixed，也不为更换标签而机械拆卡。正文和摘要都要说明核心事件属于外部现实、共同赛博经历、睡梦还是想象，不能只靠标签表达边界。'
APPROVED_PARAGRAPH='不输出 mixed，也不拆分本卡。内容包含多个经历层时按核心事件选择，并在摘要中说明核心事件属于外部现实、共同赛博经历、睡梦还是想象。'

# 2026-11-16: summary length and the experience-layer wording follow the
# self-hosted extractor ("30–50 字检索摘要", "明确……边界").
SUMMARY_BEFORE='summary 使用卡片主要语言、大约一两句话，保留关键人物、对象与事实，采用第三人称及参与者配置名字，不用“我”“你”“用户”“AI”代指参与者；逐字引语保留原样并标明说话者。配置名字不翻译，不为长度牺牲完整性。'
SUMMARY_SELF_HOSTED='summary 使用卡片主要语言，是 30–50 字的检索摘要，保留关键人物、对象与事实，用陈述句，像搜索关键词而不是讲故事；采用第三人称及参与者配置名字，不用“我”“你”“用户”“AI”代指参与者；逐字引语保留原样并标明说话者。配置名字不翻译。'
PARAGRAPH_SELF_HOSTED='不输出 mixed，也不拆分本卡。内容包含多个经历层时按核心事件选择，并在正文和摘要中明确外部现实、共同赛博经历、睡梦或想象内容的边界。'


def before_self_hosted_alignment(text):
    return text.replace(SUMMARY_SELF_HOSTED,SUMMARY_BEFORE).replace(PARAGRAPH_SELF_HOSTED,APPROVED_PARAGRAPH)

INTIMACY_SENTENCE='出现亲密或性相关的内容时，照实记录，允许直白的词汇，不含糊其辞，不用“那种事”“私密部位”“不可描述”之类的模糊代称替换或跳过；表述清楚即可，不过度铺陈细节。'


class SharedStructurePromptTests(unittest.TestCase):
    def test_chat_template_is_byte_identical_to_before_import_refactor(self):
        # Captured from 325c5ae's original extraction.md, before tokenizing it.
        current=structure_template()
        self.assertEqual(current.count(PARAGRAPH_SELF_HOSTED),1)
        self.assertEqual(current.count(SUMMARY_SELF_HOSTED),1)
        self.assertEqual(hashlib.sha256(current.encode()).hexdigest(),
            '00ec5776a89a07bd0245b72c9dc50b235924d4c89e85f5ecb92d8c7c8f553d46')
        current=before_self_hosted_alignment(current)
        self.assertEqual(current.count(INTIMACY_SENTENCE),1)
        self.assertEqual(hashlib.sha256(current.encode()).hexdigest(),
            '61483b7f659d6bf0e0914ceb0ace0e28bc589b65340e39c4b229ca5e622ceed3')
        # The approved intimacy sentence (2026-11-14) is the only addition.
        current=current.replace(INTIMACY_SENTENCE,'')
        self.assertEqual(hashlib.sha256(current.encode()).hexdigest(),
            'fd03bb0adbdcf430aa82cd3128f67cb875238c53e15ebab60358d6de1206289e')
        # Normalize only the one newly approved paragraph: the previous
        # accepted SHA still proves every other byte is unchanged.
        restored=current.replace(APPROVED_PARAGRAPH,PREVIOUS_PARAGRAPH)
        self.assertEqual(hashlib.sha256(restored.encode()).hexdigest(),
            'aec9b4099ef5f79d4740b36debbc50df8507a2c5e72b3860a55533945bed2c1f')

    def test_chat_render_is_byte_identical_including_literal_placeholders(self):
        context={'participants':{'user':{'name':'小禾'},'assistant':{'name':'林舟'}},'topics':[]}
        card={'title':'合成标题','content':'原文 {{CARD_JSON}} 保持为数据。','business_date':'2025-03-14'}
        prompt=render_prompt('structure',context,card)
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
            '8cb5844f4f02530e714b42c4ee81d353ba6c7dd150ee2c0b259df277033ec43f')
        prompt=before_self_hosted_alignment(prompt)
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
            'abcc3d791c540eeddc902e5af50df7c5fccf10f8c7a83d43f04942ab6a8608a2')
        prompt=prompt.replace(INTIMACY_SENTENCE,'')
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(),
            '6426649439754fd946b10bfd485f5b6e0152594c2c6e3c15bff9a4b5dd0b2dc7')
        restored=prompt.replace(APPROVED_PARAGRAPH,PREVIOUS_PARAGRAPH)
        self.assertEqual(hashlib.sha256(restored.encode()).hexdigest(),
            '932b6f3550bc008cfd5af6e269bf303d896cd39313e2f2d5ac19e24f2a936112')

    def test_memory_uses_same_rules_and_only_the_approved_variant_parts(self):
        chat=structure_template()
        memory=structure_template('memory')
        self.assertEqual(memory.count(PARAGRAPH_SELF_HOSTED),1)
        shared_start='二、经历层与认知状态'
        shared_end='三、时间、情感与保留层'
        self.assertEqual(chat.split(shared_start)[1].split(shared_end)[0],
            memory.split(shared_start)[1].split(shared_end)[0])
        self.assertEqual(chat.split('valence 为')[1].split('五、输出格式')[0],
            memory.split('valence 为')[1].split('五、输出格式')[0])
        material={'source_date':'2025-03-14','content':'{{ROLE}} 和 {{CARD_JSON}} 都是正文。'}
        prompt=render_prompt('memory_structure',{},material)
        self.assertIn('输出 content: null，程序会逐字保留原文',prompt)
        self.assertIn('- title、summary：非空字符串。',prompt)
        self.assertEqual(json.loads(prompt.rsplit('\n',2)[1]),material)
        directory=Path(__file__).resolve().parent.parent / 'prompts'
        rules=(directory/'card_body_rules.md').read_text().rstrip('\n')
        selection=render_prompt('selection',{},{}).replace('空间上下文 JSON：\n{}','空间上下文 JSON：\n{{SPACE_CONTEXT_JSON}}').rsplit('{}',1)[0]+'{{SOURCE_MATERIAL_JSON}}\n'
        self.assertIn(rules,selection)
        self.assertIn(rules,prompt)
        self.assertEqual(hashlib.sha256(selection.encode()).hexdigest(),
            '11c63f6737b4bfb18c1159d50833c862ae8ffcfc9547f78f9643bcb5418bf67f')

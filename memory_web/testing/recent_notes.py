"""Deterministic secretary for the disposable browser preview, with no provider."""

import asyncio
import json


class SyntheticSecretary:
    configured = True

    async def generate(self, prompt, material):
        # Brief delay makes running/cancel states observable in browser acceptance.
        await asyncio.sleep(.5)
        suggestions = []
        if '【健康1】' in material and '【健康2】' in material:
            suggestions.append(dict(type='合并', targets=['健康1', '健康2'],
                new_text='2026年11月5日 继续按医嘱用药，两周后复诊，注意休息。', reason='同一天复查后的安排合成一条。'))
        if '【长期1】' in material:
            suggestions.append(dict(type='精简', targets=['长期1'],
                new_text='2026年10月18日 开始备考，每周末复习两章。', reason='保留起始日期和复习安排。'))
        result = dict(suggestions=suggestions, need_confirm=[dict(target='健康1', question='复诊日期是否已经确定？')] if suggestions else [], note='保留重要约定，仅合并和精简重复表述。')
        return dict(content=json.dumps(result, ensure_ascii=False), model='synthetic-secretary', usage=dict(total_tokens=10))

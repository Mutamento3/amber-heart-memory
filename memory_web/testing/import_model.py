"""Synthetic-only provider and fixed model for import acceptance."""

import json
from .extraction_model import SyntheticExtractionHTTP


class SyntheticImportHTTP(SyntheticExtractionHTTP):
    def respond(self,path,body):
        if path.endswith('/embeddings'):
            return super().respond(path,body)
        prompt=body['messages'][0]['content']
        material=json.loads(prompt.rsplit('\n',2)[1])
        if 'source_date' not in material or 'title' in material:
            return super().respond(path,body)
        self.calls.append(('structure',material))
        context=json.loads(prompt.split('空间上下文 JSON：\n',1)[1].split('\n\n',1)[0])
        result=dict(title='合成记忆标题',content=None,summary='合成记忆摘要。',source_date=material['source_date'],
            experience_mode='external',cognitive_status='stable',decay_layer='tier_4_episode',
            is_milestone=False,valence=None,arousal=None,entities=[],relations=[],topic_ids=[context['topics'][0]['id']])
        return 200,dict(choices=[dict(message=dict(content=json.dumps(result)),finish_reason='stop')],
            usage=dict(prompt_tokens=12,completion_tokens=8,total_tokens=20,private='DO_NOT_SAVE'))


class SyntheticImportFixed:
    def __init__(self, *, record_calls=True):
        self.responses=SyntheticImportHTTP()
        self.record_calls=record_calls

    def extract(self,prompt):
        try:
            status,body=self.responses.respond('/v1/chat/completions',dict(messages=[dict(content=prompt)]))
            if status!=200:
                raise ValueError('synthetic import response failed')
            return body['choices'][0]['message']['content']
        finally:
            # Unit tests can inspect inputs; the load fixture must behave like
            # a real provider client, which does not retain every past prompt.
            if not self.record_calls:
                self.responses.calls.clear()

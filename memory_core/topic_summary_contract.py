"""Source checksums and the small public model response contract."""

import hashlib
import json
import re

from .extraction_contract import ContractError, check_credentials, identifier, json_object, text


class TopicSummaryFormatError(ContractError):
    code = 'topic_summary_format_invalid'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_hash(card):
    return digest({key: card[key] for key in ('id','title','content','source_date','created_at','is_milestone','primary_topic_id')})


def context_hash(policy, topic):
    return digest(dict(participants={role: {key: person[key] for key in ('name','aliases')}
        for role, person in policy['participants'].items()}, topic={key: topic[key] for key in ('id','name','preset_key')}))


def validate_response(raw, ids):
    try:
        result = json_object(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict) or set(result) != {'topic_summaries','children'}:
            raise ValueError()
        summaries, children = result['topic_summaries'], result['children']
        if not isinstance(summaries, list) or len(summaries) > 1 or not isinstance(children, list):
            raise ValueError()
        expected = set(ids)
        if len(expected) != len(ids) or not expected:
            raise ValueError()
        by_id = {}
        for child in children:
            if not isinstance(child, dict) or set(child) != {'id','status'}:
                raise ValueError()
            mid = identifier(child['id'], 'source_id')
            if mid not in expected or mid in by_id or child['status'] not in ('written','skipped'):
                raise ValueError()
            by_id[mid] = child
        if set(by_id) != expected:
            raise ValueError()
        written = [mid for mid in ids if by_id[mid]['status'] == 'written']
        if bool(written) != bool(summaries):
            raise ValueError()
        normalized = []
        if summaries:
            item = summaries[0]
            if not isinstance(item, dict) or set(item) != {'title','content'}:
                raise ValueError()
            title, content = text(item['title'],'title'), text(item['content'],'content')
            if len(title) > 300 or len(content) > 100000 or re.search(r'(\[\d+\]|#\d+)', content):
                raise ValueError()
            check_credentials([title, content])
            normalized.append(dict(title=title, content=content))
        return dict(topic_summaries=normalized, children=[by_id[mid] for mid in ids])
    except (ValueError, TypeError, KeyError):
        raise TopicSummaryFormatError('主题总结格式不符合约定，请重试') from None


def retrieval_summary(content):
    plain = ' '.join(content.partition('\n\n')[2].split())
    first = re.search(r'.*?[。！？!?]', plain)
    return (first[0] if first else plain)[:120]

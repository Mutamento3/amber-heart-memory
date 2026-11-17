"""Existing seven-field tokenization, with address-only terms from space policy."""

from collections import Counter, defaultdict
import logging
import re

import jieba


STOPWORDS = frozenset({
    '你','我','他','她','它','我们','你们','他们','她们','的','了','吗','呢','啊','呀','吧','嘛','么','哦',
    '还','又','就','都','也','很','太','更','最','一个','一些','这个','那个','这种','那种',
    '然后','因为','所以','如果','但是','而且','已经','还是','没有','不是','可以','一下',
    '时候','今天','昨天','前天','明天','最近','上周','本周','这周','上个月','这个月',
    '请','请用','用','的话','记得','回忆','告诉','讲讲','说说','发生','什么','怎么','为什么',
})
STRUCTURED_FIELDS = frozenset({'entity_name','alias','topic_name'})
EDGE_CHARS = '，。！？；：‘’“”（）()[]【】,.!?;: `"\''


def clean_address_terms(value, terms):
    for term in sorted(terms,key=len,reverse=True):
        pattern = re.escape(term)
        if term.isascii() and term.replace(' ','').isalnum():
            pattern = r'(?<![A-Za-z0-9])'+pattern+r'(?![A-Za-z0-9])'
        value = re.sub(pattern,' ',value,flags=re.IGNORECASE)
    return ' '.join(value.split())


def normalize_token(value):
    return re.sub(r'\s+',' ',str(value or '')).strip().strip(EDGE_CHARS).casefold()


def tokenize(value, ignored_terms=()):
    # The packaged dictionary is local. Do not silently change tokenizers when
    # dependencies are missing; the disposable cache lives in /tmp.
    jieba.setLogLevel(logging.WARNING)
    normalized = re.sub(r'\s+',' ',str(value or '')).strip()
    ignored = {normalize_token(t) for t in ignored_terms}
    return [token for raw in jieba.lcut_for_search(normalized)
            if (token:=normalize_token(raw)) and token not in STOPWORDS and token not in ignored
            and len(token)>=2 and not token.isdigit()]


def build_rows(memory, entities, topics, *, timestamp, ignored_terms=()):
    mid = memory['id']
    sources = {
        'title':[(mid,[memory['title']])],
        'summary':[(mid,[memory['summary']])],
        'content':[(mid,[memory['content']])],
        'entity_name':[(e['id'],[e['name']]) for e in entities],
        'entity_description':[(e['id'],[e.get('description')]) for e in entities],
        'alias':[(e['id'],e.get('aliases') or []) for e in entities],
        'topic_name':[(t['id'],[t['name']]) for t in topics],
    }
    lengths, counters = Counter(),defaultdict(list)
    ignored = {normalize_token(t) for t in ignored_terms}
    for field,items in sources.items():
        for source_id,values in items:
            seen,tokens=set(),[]
            for value in values:
                value=str(value or '').strip()
                if not value or value.casefold() in seen:
                    continue
                seen.add(value.casefold())
                tokens.extend(tokenize(value,ignored_terms))
                phrase=normalize_token(value)
                if field in STRUCTURED_FIELDS and len(phrase)>=2 and phrase!='or' and phrase not in ignored:
                    tokens.append(phrase)
            if tokens:
                counts=Counter(tokens)
                counters[field].append((source_id,counts))
                lengths[field]+=sum(counts.values())
    return [dict(field=field,source_id=source_id,term=term,term_frequency=frequency,
                 field_length=lengths[field],updated_at=timestamp)
            for field,groups in counters.items() for source_id,counts in groups for term,frequency in sorted(counts.items())]

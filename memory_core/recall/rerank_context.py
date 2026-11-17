"""The existing tail gate; participant/address exclusions come from space policy."""

import re
import threading
import time

import jieba.posseg

from ..cards.keyword_index import STOPWORDS

TAIL_NOTE = "只用于理解当前消息在回应什么，不得单独据此选记忆。若当前消息换了话题，以当前消息为准。"
_BODY_PARTS = ("屁股", "臀部", "肚子", "鼻子", "嗓子", "喉咙", "膝盖", "脚踝", "手腕", "腰部", "肩膀", "胸口")
_RHETORICAL = re.compile(r"(?:嗯+|乖|好不好|行不行|对不对|是不是|知道了?吗|明白了?吗|记住了?没|听(?:懂|见|到|清楚)(?:了)?(?:没(?:有)?|了吗))")

_GENERIC_WORDS = frozenset({"每天","一直","现在","这里","那里","这次","以后","之前","东西","事情","问题","时候","感觉","事件"})


def address_terms(policy):
    values = list(policy['ignored_terms'])
    for participant in policy['participants'].values():
        values.extend([participant['name'], *participant['aliases']])
    return frozenset(value.strip().casefold() for value in values)


def assistant_tail(messages):
    """Use only the nearest stored assistant, even if that row has no prose."""
    previous = next((m for m in reversed(messages or []) if m.get("role") == "assistant"), None)
    if previous is None:
        return ""
    text = previous.get("content") or ""
    if isinstance(text, list):
        text = " ".join(p.get("text", "") for p in text if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(text, str):
        return ""
    if text.lstrip().startswith("【调用工具】"):
        if "\n---\n" not in text:
            return ""
        text = text.rsplit("\n---\n", 1)[-1]
    if "【系统记录：这一轮已执行工具" in text and "没有生成完整回复" in text:
        return ""
    text = re.sub(r"\[语音[^\]]*\]|\[后续内容因审核被截断\]|\[[A-Za-z][A-Za-z ,\-'\x27]{0,100}\]|\[\]", "", text)
    depth, clean = 0, []
    for char in text:
        if char in "(（":
            depth += 1
        elif char in ")）" and depth:
            depth -= 1
        elif not depth:
            clean.append(char)
    text = "".join(clean).replace("|", " ").replace("*", "").replace("`", "")
    text = re.sub(r"(?m)^\s*>+\s*", "", text)
    text = " ".join(text.split()).rstrip(' >”’"』」')
    sentences = re.findall(r'[^。！？!?]+[。！？!?]+[”’"』」]*|[^。！？!?]+$', text)
    return "".join(sentences[-2:]).strip()[-120:]

def entity_terms(entities, addresses, *, full_names_only=False):
    """Names/aliases, optionally retaining legacy noun fragments and body words."""
    generic = STOPWORDS | addresses | _GENERIC_WORDS
    terms = set()
    for entity in entities:
        # Keep the identity even when its name is excluded from literal noun
        # matching, so keyword metadata can still distinguish an address.
        terms.add(((entity.get("name") or "").casefold(), entity.get("id"), entity.get("name", "")))
        if (entity.get("name") or "").casefold() in addresses:
            continue
        for value in [entity.get("name") or "", *(entity.get("aliases") or [])]:
            value = value.strip().casefold()
            candidates = [value]
            if not full_names_only:
                candidates += [word.casefold() for word, flag in jieba.posseg.cut(value)
                               if flag.startswith("n") or flag == "eng" or word in {"前端", "后端"}]
            for word in candidates:
                if len(word) >= 2 and word not in generic:
                    terms.add((word, entity.get("id"), entity.get("name", "")))
    if not full_names_only:
        terms.update((part, None, "身体部位") for part in _BODY_PARTS)
    return tuple(sorted(terms, key=lambda item: (-len(item[0]), item[0], item[1] or 0)))

def literal_matches(query, terms, addresses):
    generic = STOPWORDS | addresses | _GENERIC_WORDS
    text = query.casefold()
    words = {word.casefold() for word, _ in jieba.posseg.cut(query)}
    matches, seen = [], set()
    for word, entity_id, name in terms:
        if len(word) < 2 or word in generic or name.casefold() in addresses or word in seen:
            continue
        # Short Chinese terms must be whole query words: “扣掉发现” does not
        # mention “掉发”, nor does “不容易” mention an entity token “不容”.
        if len(word) < 4 and not word.isascii() and word not in words:
            continue
        matched = bool(re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text)) if re.fullmatch(r"[a-z0-9 _-]+", word) else word in text
        if matched:
            matches.append({"term": word, "entity_id": entity_id, "name": name})
            seen.add(word)
            if len(matches) == 5:
                break
    return matches

def has_substantive_question(tail):
    for fragment in re.findall(r"[^。！？!?]+[？?]", tail):
        clause = re.split(r"[，,：:]", fragment[:-1])[-1]
        compact = re.sub(r"[^\w\u4e00-\u9fff]", "", clause)
        if compact and not _RHETORICAL.fullmatch(compact):
            return True
    return False


class TailContext:
    def __init__(self, store):
        self.store = store
        self._cache = {}
        self._lock = threading.Lock()

    def terms(self, policy, full_names_only):
        key = (policy['revision'], full_names_only)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic()-cached[0] < 300:
                return cached[1]
            entities = self.store.entities()
            if not entities:
                raise RuntimeError('LiteralEntitiesUnavailable')
            terms = entity_terms(entities, address_terms(policy), full_names_only=full_names_only)
            # A config revision invalidates participant names, aliases and vocatives.
            self._cache = {k:v for k,v in self._cache.items() if k[0]==policy['revision']}
            self._cache[key] = (time.monotonic(), terms)
            return terms

    def full_matches(self, query, policy, keyword_entity_ids):
        addresses = address_terms(policy)
        dictionary = self.terms(policy, True)
        matches = literal_matches(query, dictionary, addresses)
        refresh_ids = sorted(set(keyword_entity_ids or ()) | {
            match['entity_id'] for match in matches if match['entity_id'] is not None})
        if not refresh_ids:
            return matches, []
        fresh = entity_terms(self.store.entities(refresh_ids), addresses, full_names_only=True)
        dictionary = tuple(t for t in dictionary if t[1] not in set(refresh_ids)) + fresh
        with self._lock:
            cached = self._cache.get((policy['revision'], True))
            if cached:
                self._cache[(policy['revision'], True)] = (cached[0], dictionary)
        return literal_matches(query, dictionary, addresses), refresh_ids

    def build(self, query, messages, policy, settings, keyword_entity_ids):
        tail = assistant_tail(messages)
        gate = settings.tail_gate_enabled
        exact = settings.exact_entity_gate_enabled
        stats = {'tail_used': False, 'gate_enabled': gate, 'reason': 'no_previous_assistant_tail',
                 'tail_chars': len(tail), 'literal_matches': [], 'keyword_entity_matches': [],
                 'substantive_question': False, 'query_chars': len(query),
                 'gate_rule': 'disabled' if not gate else ('full_entity' if exact else 'legacy'),
                 'entity_refresh_ids': []}
        if not tail:
            return query, stats
        stats['substantive_question'] = has_substantive_question(tail)
        addresses = address_terms(policy)
        if gate and exact:
            if not stats['substantive_question']:
                stats['reason'] = 'no_substantive_question'
                return query, stats
            try:
                matches, refreshed = self.full_matches(query, policy, keyword_entity_ids)
            except Exception as exc:
                stats.update(reason='literal_entities_unavailable', error=type(exc).__name__)
                return query, stats
            stats.update(literal_matches=matches, entity_refresh_ids=refreshed)
            if matches:
                stats['reason'] = 'full_entity'
                return query, stats
        elif gate:
            try:
                dictionary = self.terms(policy, False)
                matches = literal_matches(query, dictionary, addresses)
            except Exception as exc:
                stats.update(reason='literal_entities_unavailable', error=type(exc).__name__)
                return query, stats
            stats['literal_matches'] = matches
            if matches:
                stats['reason'] = 'concrete_entity'
                return query, stats
            if keyword_entity_ids is None:
                stats['reason'] = 'keyword_entities_unavailable'
                return query, stats
            names = {eid:name for _,eid,name in dictionary if eid is not None}
            generic = STOPWORDS | addresses | _GENERIC_WORDS
            keywords = [{'entity_id':eid,'name':names.get(eid) or '词表尚未刷新'}
                        for eid in dict.fromkeys(keyword_entity_ids)
                        if names.get(eid,'').casefold() not in generic][:5]
            stats['keyword_entity_matches'] = keywords
            if keywords:
                stats['reason'] = 'keyword_entity'
                return query, stats
            if not stats['substantive_question']:
                stats['reason'] = 'no_substantive_question'
                return query, stats
        stats.update(tail_used=True, reason='incomplete_reply_to_question' if gate else 'gate_disabled')
        return query+'\n\n【上一条回复结尾，仅作承接参考】\n'+tail+'\n'+TAIL_NOTE, stats

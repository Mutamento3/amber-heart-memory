"""Two-step extraction contracts. No private imports, clients, credentials or I/O."""

from copy import deepcopy
from datetime import date, datetime
import json
import math
from pathlib import Path
import re

from ..storage.clock import business_day_label


ENTITY_TYPES = ('PERSON', 'EVENT', 'PLACE', 'OBJECT', 'CONCEPT')
ENTITY_SUBTYPES = ('persona', 'health_condition', 'body_part', 'plan', 'rule', 'kink',
                   'tech_system', 'artifact', 'belief', 'identity_trait', 'medication')
SUBTYPE_TYPES = dict(persona='PERSON', artifact='OBJECT', medication='OBJECT',
                    belief='CONCEPT', identity_trait='CONCEPT', rule='CONCEPT', kink='CONCEPT')
RELATION_TYPES = ('BELONGS_TO', 'CAUSES', 'LOCATED_AT', 'HAPPENED_ON', 'LOVES', 'DISLIKES',
                  'PREFERS', 'DESIRES', 'PROMISES', 'SETS_RULE', 'VIOLATES', 'SUPPORTS',
                  'CONFLICTS_WITH', 'JUDGES', 'RELATES_TO', 'REPLACED_BY', 'CREATED',
                  'HAS_ATTRIBUTE', 'PARTICIPATES_IN')
MAX_TOPICS = 3
MODES = ('external', 'shared', 'dream', 'imagined')
COGNITIVE_STATUSES = ('stable', 'disputed')
MENTION_ROLES = ('subject', 'object', 'background')
LAYERS = ('tier_0_exempt', 'tier_1_rule', 'tier_2_reflection', 'tier_3_semantic',
          'tier_4_episode', 'tier_5_emotional')


class ContractError(ValueError):
    """Messages contain field names and codes only, never rejected content."""


class CredentialContent(ContractError):
    pass


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ContractError('nonempty_string_required:' + field)
    return value.strip()


def identifier(value, field):
    if type(value) is not int or value <= 0:
        raise ContractError('positive_integer_required:' + field)
    return value


def check_credentials(value):
    """Conservative format/context screening, not a guarantee of secret recognition."""
    encoded = json.dumps(value, ensure_ascii=False)
    patterns = (
        r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
        r'\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{20,})',
        r'(?i)(?:password|passwd|api[_ -]?key|secret|验证码|密码|密钥|证件号|身份证号|银行卡号|otp|passport(?: number)?)\s*[\"\s:=：]+[^\s\"\\,，。;；]{3,}',
        r'\b\d{17}[0-9Xx]\b',
    )
    if any(re.search(pattern, encoded) for pattern in patterns):
        raise CredentialContent('credential_content_blocked')
    for candidate in re.findall(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)', encoded):
        digits = ''.join(c for c in candidate if c.isdigit())
        if len(digits) not in range(13, 20):
            continue
        numbers = [int(c) for c in digits[::-1]]
        total = sum(n if i % 2 == 0 else n * 2 - (9 if n >= 5 else 0) for i, n in enumerate(numbers))
        if total % 10 == 0:
            raise CredentialContent('credential_content_blocked')


def json_object(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError('empty_response')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError('duplicate_json_key')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique,
                            parse_constant=lambda _: (_ for _ in ()).throw(ContractError('nonfinite_json')))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContractError('invalid_json') from None
    if not isinstance(result, dict):
        raise ContractError('object_required')
    check_credentials(result)
    return result


def keys(value, expected, field):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ContractError('invalid_fields:' + field)


def iso_date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ContractError('invalid_date')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ContractError('invalid_date') from None
    return value


def material_input(
    segments, settings, explicit_business_date=None, *, message_business_dates=None
):
    if not isinstance(segments, list) or not segments:
        raise ContractError('segments_required')
    ids, days = set(), set()
    result = []
    for segment in segments:
        keys(segment, ('segment_id', 'speaker', 'text', 'timestamp'), 'segment')
        sid = text(segment['segment_id'], 'segment_id')
        if sid in ids:
            raise ContractError('duplicate_segment_id')
        ids.add(sid)
        speaker = text(segment['speaker'], 'speaker')
        if speaker not in ('user', 'assistant'):
            raise ContractError('invalid_speaker')
        stamp = segment['timestamp']
        if stamp is not None:
            try:
                instant = datetime.fromisoformat(stamp)
                actual_day = business_day_label(instant, settings).isoformat()
                # Trusted conversation callers already know the whole round's
                # day. Preserve actual timestamps even across that boundary.
                days.add(
                    iso_date(message_business_dates[sid])
                    if message_business_dates is not None
                    else actual_day
                )
            except (ValueError, TypeError, KeyError):
                raise ContractError('offset_timestamp_required') from None
        elif explicit_business_date is None:
            raise ContractError('missing_time_requires_business_date')
        text(segment['text'], 'segment_text')
        result.append(dict(segment_id=sid, speaker=speaker, text=segment['text'], timestamp=stamp))
    if message_business_dates is not None and (
        not isinstance(message_business_dates, dict)
        or set(message_business_dates) != ids
    ):
        raise ContractError("invalid_message_day_mapping")
    if explicit_business_date is not None:
        days.add(iso_date(explicit_business_date))
    if len(days) != 1:
        raise ContractError('split_material_by_business_day')
    check_credentials(result)
    return result, days.pop()


def evidence(value, segments):
    if not isinstance(value, list) or not value:
        raise ContractError('evidence_required')
    source = {s['segment_id']: s['text'] for s in segments}
    for item in value:
        keys(item, ('segment_id', 'quote'), 'evidence')
        text(item['quote'], 'quote')
        quote = item['quote']
        sid = text(item['segment_id'], 'segment_id')
        if sid not in source or quote not in source[sid]:
            raise ContractError('evidence_not_in_source')


def narrative(value, segments, *, edited=False):
    keys(value, ('title', 'content', 'summary', 'importance', 'evidence'), 'narrative')
    result = deepcopy(value)
    for field in ('title', 'content', 'summary'):
        result[field] = text(value[field], field)
    score = value['importance']
    if type(score) is not int or not (0 if edited else 1) <= score <= 10:
        raise ContractError('invalid_importance')
    evidence(value['evidence'], segments)
    check_credentials(result)
    return result


def selection_result(raw, segments):
    value = json_object(raw)
    keys(value, ('memories', 'skipped_reason'), 'selection')
    if not isinstance(value['memories'], list):
        raise ContractError('memories_array_required')
    if value['memories']:
        if value['skipped_reason'] is not None:
            raise ContractError('unexpected_skip_reason')
    else:
        text(value['skipped_reason'], 'skipped_reason')
    return [narrative(item, segments) for item in value['memories']], value['skipped_reason']


def structure(value, context):
    fields = ('summary', 'experience_mode', 'cognitive_status', 'decay_layer', 'source_date',
              'is_milestone', 'valence', 'arousal', 'entities', 'relations', 'topic_ids')
    keys(value, fields, 'structure')
    result = deepcopy(value)
    text(result['summary'], 'summary')
    for field, allowed in (('experience_mode', MODES), ('cognitive_status', COGNITIVE_STATUSES), ('decay_layer', LAYERS)):
        if result[field] not in allowed:
            raise ContractError('invalid_enum:' + field)
    iso_date(result['source_date'])
    if type(result['is_milestone']) is not bool:
        raise ContractError('invalid_milestone')
    for field, low in (('valence', -1), ('arousal', 0)):
        number = result[field]
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= 1):
            raise ContractError('invalid_emotion:' + field)
    if not isinstance(result['entities'], list) or not isinstance(result['relations'], list):
        raise ContractError('graph_arrays_required')
    known = {e['id']: e for e in context['known_entities']}
    refs = {}
    for entity in result['entities']:
        keys(entity, ('ref', 'name', 'known_entity_id', 'entity_type', 'entity_subtype', 'aliases', 'mention_role', 'description'), 'entity')
        ref = text(entity['ref'], 'ref')
        if ref in refs:
            raise ContractError('duplicate_entity_ref')
        text(entity['name'], 'entity_name')
        if entity['entity_type'] not in ENTITY_TYPES:
            raise ContractError('invalid_entity_type')
        if not isinstance(entity['entity_subtype'], (str,type(None))):
            raise ContractError('invalid_entity_subtype')
        if entity['entity_subtype'] not in (*ENTITY_SUBTYPES, None):
            entity['entity_subtype'] = None
        if entity['entity_subtype'] in SUBTYPE_TYPES:
            entity['entity_type'] = SUBTYPE_TYPES[entity['entity_subtype']]
        eid = entity['known_entity_id']
        if eid is not None:
            identifier(eid, 'known_entity_id')
            if eid not in known:
                raise ContractError('unknown_or_cross_space_entity')
            entity['name'] = known[eid]['name']
            entity['entity_type'] = known[eid]['entity_type']
            entity['entity_subtype'] = known[eid]['entity_subtype']
            if entity['entity_subtype'] not in (*ENTITY_SUBTYPES,None) or (entity['entity_subtype'] in SUBTYPE_TYPES and SUBTYPE_TYPES[entity['entity_subtype']]!=entity['entity_type']):
                raise ContractError('known_entity_type_conflict')
        if not isinstance(entity['aliases'], list) or any(not isinstance(a, str) or not a.strip() for a in entity['aliases']):
            raise ContractError('invalid_aliases')
        if entity['mention_role'] not in MENTION_ROLES:
            raise ContractError('invalid_mention_role')
        text(entity['description'], 'entity_description')
        refs[ref] = entity
    for relation in result['relations']:
        keys(relation, ('source_ref', 'target_ref', 'relation_type', 'relation_type_raw', 'description', 'valid_from'), 'relation')
        text(relation['source_ref'], 'source_ref')
        text(relation['target_ref'], 'target_ref')
        if relation['source_ref'] not in refs or relation['target_ref'] not in refs:
            raise ContractError('dangling_relation')
        text(relation['relation_type'], 'relation_type')
        if relation['relation_type_raw'] is not None:
            text(relation['relation_type_raw'], 'relation_type_raw')
        if relation['relation_type'] not in RELATION_TYPES or (relation['relation_type']=='PARTICIPATES_IN' and refs[relation['target_ref']]['entity_type']!='EVENT'):
            relation['relation_type_raw'] = relation['relation_type_raw'] or relation['relation_type']
            relation['relation_type'] = 'RELATES_TO'
        text(relation['description'], 'relation_description')
        iso_date(relation['valid_from'])
    result['topic_ids']=validate_topics(result['topic_ids'],context)
    check_credentials(result)
    return result


def validate_topics(ids, context):
    topics = {t['id']: t for t in context['topics']}
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_TOPICS or any(type(i) is not int or i not in topics for i in ids) or len(set(ids)) != len(ids):
        raise ContractError('invalid_or_cross_space_topics')
    normal = [i for i in ids if not topics[i]['is_fallback']]
    return normal or ids[:1]


def structure_template(kind='chat'):
    directory = Path(__file__).parent.parent / 'prompts'
    variants = json.loads((directory / 'structure_variants.json').read_text(encoding='utf-8'))
    if kind not in variants:
        raise ValueError('unknown_material_kind')
    template = (directory / 'extraction.md').read_text(encoding='utf-8')
    return with_card_body_rules(directory, re.sub(r'\{\{([A-Z_]+)\}\}',
        lambda match: variants[kind].get(match[1], match[0]), template))


def with_card_body_rules(directory, template):
    if '{{CARD_BODY_RULES}}' in template:
        return template.replace('{{CARD_BODY_RULES}}', (directory / 'card_body_rules.md').read_text(encoding='utf-8').rstrip('\n'))
    return template


def render_prompt(stage, context, material):
    if stage not in ('selection','structure','memory_structure'):
        raise ValueError('unknown_prompt_stage')
    if stage == 'selection':
        token = '{{SOURCE_MATERIAL_JSON}}'
        directory = Path(__file__).parent.parent / 'prompts'
        template = with_card_body_rules(directory, (directory / 'selection.md').read_text(encoding='utf-8'))
    else:
        token = '{{CARD_JSON}}'
        template = structure_template('memory' if stage == 'memory_structure' else 'chat')
    # Replace in one pass: literal placeholders in untrusted material stay data.
    values = {'{{SPACE_CONTEXT_JSON}}': context, token: material}
    return re.sub(r'\{\{(?:SPACE_CONTEXT_JSON|SOURCE_MATERIAL_JSON|CARD_JSON)\}\}',
                  lambda match: json.dumps(values[match[0]], ensure_ascii=False, sort_keys=True), template)

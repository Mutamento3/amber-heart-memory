"""Owner-managed participant and topic configuration, stored once in Postgres."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path

import psycopg

from .db import connect
from .schema import ACQUAINTANCE_DATE_VERSION, CONVERSATION_VERSION, GRAPH_VERSION, TOPIC_DETAILS_VERSION, TOPIC_PRESET_VERSION, SUPPORTED_VERSIONS

_UNSET = object()


class RevisionConflict(ValueError):
    pass


@dataclass(frozen=True)
class Participant:
    name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("参与者名字不能为空")
        if not isinstance(self.aliases, (tuple, list)) or any(not isinstance(a, str) or not a.strip() for a in self.aliases):
            raise ValueError("别名必须是非空字符串列表")


def topic_presets():
    values = json.loads((Path(__file__).parent.parent / "presets" / "topics.json").read_text(encoding="utf-8"))
    for topics in values.values():
        if (sum(bool(t.get("is_fallback")) for t in topics) != 1
            or len({t["name"] for t in topics}) != len(topics)
            or len({t["key"] for t in topics}) != len(topics)):
            raise ValueError("主题模板需要不重复的名称和唯一兜底项")
    return values


def default_ignored_terms(template):
    values = json.loads((Path(__file__).parent.parent / "presets" / "ignored_terms.json").read_text(encoding="utf-8"))
    return list(values[template])


def read_topics(connection, space_id, *, policy=None):
    policy = policy or read_policy(connection, space_id)
    topics = connection.execute("SELECT * FROM memory_core.topics WHERE bot_id=%s ORDER BY id", (space_id,)).fetchall()
    presets = {topic["key"]: topic for topic in topic_presets()[policy["topic_template"]]}
    for topic in topics:
        if "preset_key" in topic:
            if topic["preset_key"] not in presets:
                raise ValueError("主题模板项不存在，请检查程序版本与模板文件")
            topic["description"] = presets[topic["preset_key"]]["description"]
        else:
            # Supported older schema versions still contain their original copy.
            topic["description"] = topic.get("description") or ""
    return topics


def read_policy(connection, space_id):
    connection.execute("SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))", ("space-config:" + space_id,))
    policy = connection.execute("SELECT * FROM memory_core.space_policies WHERE bot_id=%s", (space_id,)).fetchone()
    if not policy or policy["user_entity_id"] is None:
        raise ValueError("记忆空间尚未配置参与者")
    participants = {}
    for role in ("user", "assistant"):
        entity = connection.execute("SELECT id,name,aliases FROM memory_core.entities WHERE id=%s AND bot_id=%s",
            (policy[role + "_entity_id"], space_id)).fetchone()
        if not entity:
            raise ValueError("参与者引用无效")
        participants[role] = entity
    return {**policy, "participants": participants,
        "blocked_seed_entity_ids": [policy["user_entity_id"], policy["assistant_entity_id"]]}


class SpaceConfiguration:
    def __init__(self, dsn):
        self.dsn = dsn
        with connect(dsn) as connection:
            row = connection.execute("""SELECT current_user AS actor,pg_get_userbyid(nspowner) AS owner
                FROM pg_namespace WHERE nspname='memory_core'""").fetchone()
            if not row or row["actor"] != row["owner"]:
                raise ValueError("配置修改必须使用迁移/配置所有者账号")
            record = connection.execute("SELECT version FROM memory_core.installation").fetchone()
            self.version = record["version"]
            if record["version"] not in SUPPORTED_VERSIONS or record["version"] < GRAPH_VERSION:
                raise ValueError("请先升级到空间配置支持的数据库版本")

    @staticmethod
    def _lock(connection, space_id, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("配置修订号必须是非负整数")
        connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("space-config:" + space_id,))
        policy = connection.execute("SELECT * FROM memory_core.space_policies WHERE bot_id=%s FOR UPDATE", (space_id,)).fetchone()
        actual = policy["revision"] if policy else 0
        if actual != expected_revision:
            raise RevisionConflict("配置已改变，请重新读取后确认")
        return policy

    @staticmethod
    def _reindex_space(connection, space_id, ignored_terms):
        from psycopg.types.json import Jsonb
        from ..cards.keyword_index import build_rows

        # Configuration and the seven-field keyword index must commit together.
        # The exclusive policy lock also serializes this with reviewed ingestion.
        cards = connection.execute('SELECT * FROM memory_core.memory_items WHERE bot_id=%s ORDER BY id FOR UPDATE', (space_id,)).fetchall()
        for card in cards:
            entities = connection.execute('''SELECT e.* FROM memory_core.entities e
                JOIN memory_core.memory_entities me ON me.bot_id=e.bot_id AND me.entity_id=e.id
                WHERE me.bot_id=%s AND me.memory_id=%s ORDER BY e.id''', (space_id,card['id'])).fetchall()
            topics = connection.execute('''SELECT t.* FROM memory_core.topics t
                JOIN memory_core.memory_topics mt ON mt.bot_id=t.bot_id AND mt.topic_id=t.id
                WHERE mt.bot_id=%s AND mt.memory_id=%s ORDER BY t.id''', (space_id,card['id'])).fetchall()
            timestamp = connection.execute('SELECT memory_core.format_timestamp(CURRENT_TIMESTAMP) AS value').fetchone()['value']
            rows = build_rows(card,entities,topics,timestamp=timestamp,ignored_terms=ignored_terms)
            connection.execute('SELECT memory_core.replace_memory_keyword_tokens(%s,%s,%s)', (card['id'],space_id,Jsonb(rows)))

    def configure(
        self,
        space_id,
        user: Participant,
        assistant: Participant,
        *,
        ignored_terms=_UNSET,
        source_retention_days=None,
        topic_template=None,
        expected_revision=0,
        acquaintance_date=_UNSET,
        conversation_retention_days=_UNSET,
        single_space=False,
    ):
        if not isinstance(space_id, str) or not space_id.strip():
            raise ValueError("空间标识不能为空")
        if not isinstance(user, Participant) or not isinstance(assistant, Participant):
            raise TypeError("必须明确提供用户和 AI 参与者")
        if ignored_terms is not _UNSET and (not isinstance(ignored_terms, (tuple, list)) or any(not isinstance(t, str) or not t.strip() for t in ignored_terms)):
            raise ValueError("纯称呼词必须是非空字符串列表")
        if source_retention_days is not None and (type(source_retention_days) is not int or source_retention_days <= 0):
            raise ValueError("保留天数必须是正整数；未设置表示永久保留")
        if conversation_retention_days is not _UNSET and conversation_retention_days is not None and (type(conversation_retention_days) is not int or conversation_retention_days <= 0):
            raise ValueError('对话保留天数必须是正整数；null 表示永久保留')
        with connect(self.dsn) as connection:
            if single_space and expected_revision == 0:
                connection.execute("SELECT pg_advisory_xact_lock(7242261014)")
                if connection.execute(
                    "SELECT 1 FROM memory_core.space_policies WHERE bot_id<>%s AND user_entity_id IS NOT NULL LIMIT 1",
                    (space_id,),
                ).fetchone():
                    raise ValueError(
                        "一套记忆库只服务一个 AI 助手；请为另一个助手单独部署一套"
                    )
            version = connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
            if conversation_retention_days is not _UNSET and version < CONVERSATION_VERSION:
                raise ValueError('对话保留设置需要先升级 memory_core 到版本 10')
            if acquaintance_date is not _UNSET:
                self._validate_acquaintance_date(acquaintance_date, version)
            previous = self._lock(connection, space_id, expected_revision)
            default = connection.execute("SELECT default_topic_template FROM memory_core.installation").fetchone()["default_topic_template"]
            template = topic_template or (previous["topic_template"] if previous else default)
            presets = topic_presets()
            if template not in presets:
                raise ValueError("未知主题模板")
            if previous and template != previous["topic_template"]:
                raise ValueError("主题模板在创建空间后锁定，不能切换")
            if ignored_terms is _UNSET:
                ignored_terms = previous["ignored_terms"] if previous else (
                    default_ignored_terms(template) if version >= TOPIC_PRESET_VERSION else [])
            keys = [set(connection.execute("SELECT memory_core.entity_lookup_keys(%s,%s) AS keys", (p.name, list(p.aliases))).fetchone()["keys"]) for p in (user, assistant)]
            ignored_keys = set(connection.execute("SELECT memory_core.entity_lookup_keys('',%s) AS keys", (list(ignored_terms),)).fetchone()["keys"])
            if keys[0] & keys[1] or (keys[0] | keys[1]) & ignored_keys:
                raise ValueError("两个参与者的名字/别名不能重叠，纯称呼词不能作为参与者别名")
            if previous is None:
                connection.execute("INSERT INTO memory_core.space_policies(bot_id,topic_template) VALUES (%s,%s)", (space_id, template))
            ids = []
            for role, participant in (("user", user), ("assistant", assistant)):
                entity = connection.execute("""INSERT INTO memory_core.entities
                    (bot_id,entity_key,name,aliases,entity_type) VALUES (%s,%s,%s,%s,'PERSON')
                    ON CONFLICT (bot_id,entity_key) DO UPDATE SET name=excluded.name,aliases=excluded.aliases,
                        updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) RETURNING id""",
                    (space_id, "participant:" + role, participant.name.strip(), list(participant.aliases))).fetchone()
                ids.append(entity["id"])
            connection.execute("""UPDATE memory_core.space_policies SET user_entity_id=%s,assistant_entity_id=%s,
                ignored_terms=%s,source_retention_days=%s,revision=%s WHERE bot_id=%s""",
                (*ids, list(ignored_terms), source_retention_days, expected_revision + 1, space_id))
            # Omitted metadata must survive saves from older callers and pages.
            if acquaintance_date is not _UNSET:
                connection.execute("UPDATE memory_core.space_policies SET acquaintance_date=%s WHERE bot_id=%s",
                    (acquaintance_date, space_id))
            if conversation_retention_days is not _UNSET:
                connection.execute('UPDATE memory_core.space_policies SET conversation_retention_days=%s WHERE bot_id=%s',
                                   (conversation_retention_days, space_id))
            if previous is None:
                for topic in presets[template]:
                    if version >= TOPIC_PRESET_VERSION:
                        connection.execute("INSERT INTO memory_core.topics(bot_id,name,is_fallback,preset_key) VALUES (%s,%s,%s,%s)",
                            (space_id,topic["name"],topic.get("is_fallback",False),topic["key"]))
                    elif version >= TOPIC_DETAILS_VERSION:
                        connection.execute("INSERT INTO memory_core.topics(bot_id,name,is_fallback,description) VALUES (%s,%s,%s,%s)",
                            (space_id,topic["name"],topic.get("is_fallback",False),topic.get("description","")))
                    else:
                        connection.execute("INSERT INTO memory_core.topics(bot_id,name,is_fallback) VALUES (%s,%s,%s)",
                            (space_id,topic["name"],topic.get("is_fallback",False)))
            if previous is not None:
                self._reindex_space(connection,space_id,ignored_terms)
            return read_policy(connection, space_id)

    def _validate_acquaintance_date(self, value, version):
        from ..cards.extraction_contract import iso_date

        if version < ACQUAINTANCE_DATE_VERSION:
            raise ValueError("相识日需要先升级 memory_core 到版本 8")
        try:
            iso_date(value)
        except ValueError:
            raise ValueError("相识日须为有效的 YYYY-MM-DD 日期或 null") from None

    def set_acquaintance_date(self, space_id, *, expected_revision, acquaintance_date):
        with connect(self.dsn) as connection:
            version = connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
            self._validate_acquaintance_date(acquaintance_date, version)
            policy = self._lock(connection, space_id, expected_revision)
            if not policy:
                raise ValueError("空间尚未配置")
            connection.execute("""UPDATE memory_core.space_policies
                SET acquaintance_date=%s,revision=revision+1,
                    updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s""",
                (acquaintance_date, space_id))
            return read_policy(connection, space_id)

    def set_persona(self, space_id, *, expected_revision, persona):
        from .schema import HOSTED_CONTEXT_VERSION

        if not isinstance(persona, str) or len(persona) > 100000:
            raise ValueError("人设须为 100000 字以内的文字；留空沿用前端系统提示词")
        with connect(self.dsn) as connection:
            version = connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
            if version < HOSTED_CONTEXT_VERSION:
                raise ValueError("人设需要先升级 memory_core 到版本 11")
            if not self._lock(connection, space_id, expected_revision):
                raise ValueError("空间尚未配置")
            connection.execute("""UPDATE memory_core.space_policies SET persona=%s,revision=revision+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s""",
                (persona if persona.strip() else "", space_id))
            return read_policy(connection, space_id)

    def set_review_mode(self, space_id, *, expected_revision, review_mode):
        from .schema import DAILY_EXTRACTION_VERSION, DELAYED_APPROVAL_VERSION

        if review_mode not in ("auto", "manual"):
            raise ValueError("审核方式必须是自动通过或逐张审核")
        with connect(self.dsn) as connection:
            version = connection.execute(
                "SELECT version FROM memory_core.installation"
            ).fetchone()["version"]
            if version >= DELAYED_APPROVAL_VERSION:
                raise ValueError("旧审核方式已停用，请使用到期自动入库开关")
            if version < DAILY_EXTRACTION_VERSION:
                raise ValueError("审核方式需要先升级记忆核心到版本 14")
            if not self._lock(connection, space_id, expected_revision):
                raise ValueError("空间尚未配置")
            connection.execute(
                """UPDATE memory_core.space_policies SET review_mode=%s,revision=revision+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s""",
                (review_mode, space_id),
            )
            return read_policy(connection, space_id)


    def set_auto_approval(self, space_id, *, expected_revision, enabled):
        from .schema import DELAYED_APPROVAL_VERSION
        if type(enabled) is not bool:
            raise ValueError('到期自动入库开关必须是布尔值')
        with connect(self.dsn) as connection:
            if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'] < DELAYED_APPROVAL_VERSION:
                raise ValueError('到期自动入库需要先升级记忆核心到版本 16')
            if not self._lock(connection,space_id,expected_revision):
                raise ValueError('空间尚未配置')
            connection.execute('''UPDATE memory_core.space_policies SET auto_approve_enabled=%s,revision=revision+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s''', (enabled,space_id))
            self._advance_switch_revision(connection, space_id, expected_revision)
            return read_policy(connection,space_id)

    def set_period_notes(self, space_id, *, expected_revision, enabled):
        from .schema import PERIOD_NOTES_SWITCH_VERSION
        if type(enabled) is not bool:
            raise ValueError('月经周期开关必须是布尔值')
        with connect(self.dsn) as connection:
            if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'] < PERIOD_NOTES_SWITCH_VERSION:
                raise ValueError(f'月经周期开关需要先升级记忆核心到版本 {PERIOD_NOTES_SWITCH_VERSION}')
            if not self._lock(connection,space_id,expected_revision):
                raise ValueError('空间尚未配置')
            connection.execute('''UPDATE memory_core.space_policies SET period_notes_enabled=%s,revision=revision+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s''', (enabled,space_id))
            self._advance_switch_revision(connection, space_id, expected_revision)
            return read_policy(connection,space_id)

    @staticmethod
    def _advance_switch_revision(connection, space_id, expected_revision):
        from .schema import TOPIC_SUMMARY_VERSION
        # Only a switch changed: advance current drafts, never silently
        # accept an older semantic configuration or reset the waiting clock.
        connection.execute('''UPDATE memory_core.pending_memories SET policy_revision=%s,version=version+1
            WHERE bot_id=%s AND policy_revision=%s AND status IN ('pending','structure_pending','structure_failed')''',
            (expected_revision+1,space_id,expected_revision))
        connection.execute('''UPDATE memory_core.material_extractions SET policy_revision=%s,version=version+1
            WHERE bot_id=%s AND policy_revision=%s''',(expected_revision+1,space_id,expected_revision))
        if connection.execute('SELECT version FROM memory_core.installation').fetchone()['version'] >= TOPIC_SUMMARY_VERSION:
            connection.execute('''UPDATE memory_core.topic_summary_batches SET policy_revision=%s,version=version+1
                WHERE bot_id=%s AND policy_revision=%s AND status IN ('pending','generating','failed')''',
                (expected_revision+1,space_id,expected_revision))

    def edit_topic(self, space_id, topic_id, *, expected_revision, name=None, summary_eligible=None, description=None):
        with connect(self.dsn) as connection:
            version = connection.execute("SELECT version FROM memory_core.installation").fetchone()["version"]
        if version >= TOPIC_PRESET_VERSION and (name is not None or description is not None):
            raise ValueError("主题名称和说明由模板固定，不能修改")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise ValueError("主题名不能为空")
        if summary_eligible is not None and type(summary_eligible) is not bool:
            raise ValueError("整理资格必须为布尔值")
        if description is not None:
            if not isinstance(description,str) or len(description)>1000:
                raise ValueError("主题说明须为 1000 字以内的文字")
            from ..cards.extraction_contract import check_credentials
            check_credentials(description)
            if version < TOPIC_DETAILS_VERSION:
                raise ValueError("请先升级到主题说明支持的版本")
        with connect(self.dsn) as connection:
            policy = self._lock(connection, space_id, expected_revision)
            if not policy:
                raise ValueError("空间尚未配置")
            topic = connection.execute("SELECT * FROM memory_core.topics WHERE id=%s AND bot_id=%s", (topic_id, space_id)).fetchone()
            if not topic:
                raise ValueError("主题不存在或不属于该空间")
            connection.execute("UPDATE memory_core.topics SET name=%s,summary_eligible=%s WHERE id=%s AND bot_id=%s",
                (name.strip() if name is not None else topic["name"], summary_eligible if summary_eligible is not None else topic["summary_eligible"], topic_id, space_id))
            if description is not None:
                connection.execute('UPDATE memory_core.topics SET description=%s WHERE id=%s AND bot_id=%s', (description.strip(),topic_id,space_id))
            connection.execute("UPDATE memory_core.space_policies SET revision=revision+1 WHERE bot_id=%s", (space_id,))
            if name is not None and name.strip() != topic['name']:
                self._reindex_space(connection,space_id,policy['ignored_terms'])
            return read_policy(connection, space_id)

    def set_core_memory(self, space_id, *, expected_revision, core_memory):
        from ..cards.extraction_contract import check_credentials
        from .schema import CORE_MEMORY_VERSION

        if not isinstance(core_memory, str) or len(core_memory)>5000:
            raise ValueError('核心记忆须为 5000 字以内的文字')
        check_credentials(core_memory)
        with connect(self.dsn) as connection:
            version = connection.execute('SELECT version FROM memory_core.installation').fetchone()['version']
            if version < CORE_MEMORY_VERSION:
                raise ValueError('核心记忆需要先升级记忆核心到版本 21')
            if not self._lock(connection, space_id, expected_revision):
                raise ValueError('空间尚未配置')
            connection.execute('''UPDATE memory_core.space_policies SET core_memory=%s,revision=revision+1,
                updated_at=memory_core.format_timestamp(CURRENT_TIMESTAMP) WHERE bot_id=%s''',
                (core_memory if core_memory.strip() else '', space_id))
            return read_policy(connection, space_id)

    def set_extraction_rules(self, space_id, *, expected_revision, minimum_importance=None, daily_limit=None):
        if minimum_importance is not None and (type(minimum_importance) is not int or not 1<=minimum_importance<=10):
            raise ValueError("筛选最低评分必须是 1–10 的整数")
        if daily_limit is not None and (type(daily_limit) is not int or not 0<daily_limit<=2147483647):
            raise ValueError("每日条数必须是正整数")
        with connect(self.dsn) as connection:
            policy = self._lock(connection,space_id,expected_revision)
            if not policy or 'minimum_importance' not in policy:
                raise ValueError("请先安装材料/草稿版本并配置空间")
            connection.execute('''UPDATE memory_core.space_policies SET minimum_importance=%s,daily_limit=%s,
                revision=revision+1 WHERE bot_id=%s''', (minimum_importance if minimum_importance is not None else policy['minimum_importance'],
                daily_limit if daily_limit is not None else policy['daily_limit'],space_id))
            return read_policy(connection,space_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Configure a memory space using a separate owner connection")
    parser.add_argument("--space", required=True)
    parser.add_argument("--user-name", required=True)
    parser.add_argument("--user-alias", action="append", default=[])
    parser.add_argument("--assistant-name", required=True)
    parser.add_argument("--assistant-alias", action="append", default=[])
    terms = parser.add_mutually_exclusive_group()
    terms.add_argument("--ignored-term", action="append", help="覆盖默认称呼清单；可重复传入，省略则保留已有值")
    terms.add_argument("--clear-ignored-terms", action="store_true", help="清空搜索时忽略的称呼")
    parser.add_argument("--retention-days", type=int)
    conversation_retention = parser.add_mutually_exclusive_group()
    conversation_retention.add_argument('--conversation-retention-days', type=int)
    conversation_retention.add_argument('--conversation-retention-forever', action='store_true')
    parser.add_argument("--topic-template", choices=("lover", "general"))
    parser.add_argument("--expected-revision", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get("MEMORY_CONFIG_DSN")
        if not dsn:
            raise ValueError("未设置 MEMORY_CONFIG_DSN")
        policy = SpaceConfiguration(dsn).configure(args.space,
            Participant(args.user_name, args.user_alias), Participant(args.assistant_name, args.assistant_alias),
            ignored_terms=[] if args.clear_ignored_terms else (args.ignored_term if args.ignored_term is not None else _UNSET), source_retention_days=args.retention_days,
            topic_template=args.topic_template, expected_revision=args.expected_revision,
            conversation_retention_days=None if args.conversation_retention_forever else (
                args.conversation_retention_days if args.conversation_retention_days is not None else _UNSET))
    except ValueError as exc:
        parser.exit(2, f"配置停止：{exc}\n")
    except psycopg.Error as exc:
        # Driver diagnostics may include a failed row containing private names.
        parser.exit(2, f"配置停止：数据库校验或权限检查失败（SQLSTATE {exc.sqlstate or 'unknown'}）\n")
    print(json.dumps({"revision": policy["revision"], "configured_participant_roles": ["user", "assistant"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

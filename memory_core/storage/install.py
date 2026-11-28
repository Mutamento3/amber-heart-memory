"""Versioned, transactional installation for a dedicated Postgres database."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .clock import format_timestamp
from .config import Settings
from .db import connect
from .schema import DRAFT_POLICY_ALIGNMENT_VERSION, LATEST_VERSION, MIGRATION_FILES, SUPPORTED_VERSIONS


MIGRATION = Path(__file__).parent.parent / "migrations" / "001_core.sql"
MIGRATIONS = MIGRATION.parent


class InstallationMismatch(RuntimeError):
    """An existing installation needs explicit investigation or migration."""


def _rebase_import_digests(connection):
    from ..cards.drafts import context_digest, context_for

    # The old hash mixed in core memory and cannot be converted. Use the
    # settings at upgrade time as the baseline for jobs that can still run.
    groups = connection.execute("""SELECT j.bot_id, array_agg(j.id) AS job_ids
        FROM memory_core.import_jobs j
        WHERE j.status IN ('preview', 'queued', 'running', 'failed')
           OR (j.status = 'completed' AND EXISTS (
               SELECT 1 FROM memory_core.import_items i
               WHERE (i.job_id, i.bot_id) = (j.id, j.bot_id)
                 AND i.selected AND i.status = 'failed'))
        GROUP BY j.bot_id ORDER BY j.bot_id""").fetchall()
    for group in groups:
        space_id = group['bot_id']
        digest = context_digest(context_for(connection, space_id))
        connection.execute("""UPDATE memory_core.import_jobs SET context_digest=%s
            WHERE bot_id=%s AND id=ANY(%s)""", (digest, space_id, group['job_ids']))


def catalog_fingerprint(connection):
    """Hash definitions and grants, excluding data and sequence current values."""
    queries = {
        "schema": """SELECT nspname, pg_get_userbyid(nspowner) AS owner, nspacl::text
            FROM pg_namespace WHERE nspname = 'memory_core'""",
        "relations": """SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner,
            c.relrowsecurity, c.relforcerowsecurity, c.relacl::text, c.reloptions
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory_core' ORDER BY c.relname""",
        "columns": """SELECT c.relname, a.attname, a.attnum,
            format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull,
            a.attidentity, a.attgenerated, a.attacl::text,
            pg_get_expr(d.adbin,d.adrelid) AS default_expression
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            WHERE n.nspname='memory_core' AND a.attnum>0 AND NOT a.attisdropped
            AND c.relkind IN ('r','S','v','m') ORDER BY c.relname,a.attnum""",
        "constraints": """SELECT c.relname, x.conname, x.contype, x.convalidated,
            pg_get_constraintdef(x.oid) AS definition FROM pg_constraint x
            JOIN pg_class c ON c.oid=x.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory_core' ORDER BY c.relname,x.conname""",
        "indexes": """SELECT c.relname, pg_get_indexdef(i.indexrelid) AS definition,
            i.indisvalid,i.indisready FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_core'
            ORDER BY c.relname""",
        "functions": """SELECT p.proname, pg_get_functiondef(p.oid) AS definition,
            pg_get_userbyid(p.proowner) AS owner,p.proacl::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='memory_core' ORDER BY p.proname,p.oid::regprocedure::text""",
        "triggers": """SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) AS definition
            FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='memory_core' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname""",
        "sequences": """SELECT sequencename,data_type,start_value,min_value,max_value,increment_by,cycle
            FROM pg_sequences WHERE schemaname='memory_core' ORDER BY sequencename""",
        "extension": """SELECT e.extname,e.extversion,n.nspname FROM pg_extension e
            JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'""",
    }
    definitions = {name: connection.execute(query).fetchall() for name, query in queries.items()}
    encoded = json.dumps(definitions, sort_keys=True, ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _grants(connection, application_role, version):
    app = sql.Identifier(application_role)
    statements = [
        "GRANT USAGE ON SCHEMA memory_core TO {}",
        "GRANT SELECT ON memory_core.installation TO {}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON memory_core.memory_items, memory_core.memory_keyword_tokens TO {}",
        "GRANT USAGE ON ALL SEQUENCES IN SCHEMA memory_core TO {}",
        "GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA memory_core TO {}",
    ]
    if version >= 2:
        statements.append("GRANT SELECT ON memory_core.schema_migrations TO {}")
    if version >= 3:
        statements.extend([
            "GRANT SELECT ON memory_core.space_policies, memory_core.topics TO {}",
            "GRANT SELECT, INSERT, UPDATE, DELETE ON memory_core.entities, memory_core.relations, memory_core.memory_entities, memory_core.memory_topics TO {}",
        ])
    if version >= 4:
        statements.append("GRANT SELECT, INSERT, UPDATE ON memory_core.source_materials, memory_core.material_extractions, memory_core.pending_memories TO {}")
    if version >= 10:
        statements.append("GRANT SELECT, INSERT, UPDATE ON memory_core.conversation_turns, memory_core.conversation_requests, memory_core.conversation_messages, memory_core.conversation_tool_events, memory_core.conversation_space_state TO {}")
    if version >= 12:
        statements.append(
            "GRANT SELECT, INSERT, UPDATE ON memory_core.conversation_summaries, memory_core.conversation_summary_turns TO {}"
        )
    if version >= 13:
        statements.append(
            "GRANT SELECT, INSERT, UPDATE ON memory_core.conversation_diaries, memory_core.daily_tasks TO {}"
        )
    if version >= 18:
        statements.append("GRANT SELECT, INSERT, UPDATE ON memory_core.topic_summary_batches, memory_core.topic_summary_sources TO {}")
    if version >= 20:
        statements.append("GRANT SELECT, INSERT, UPDATE ON memory_core.recent_notes TO {}")
        statements.append("GRANT SELECT, INSERT, UPDATE, DELETE ON memory_core.moment_bookmarks TO {}")
    if version >= 22:
        statements.append("GRANT SELECT, INSERT, UPDATE ON memory_core.import_jobs, memory_core.import_items TO {}")
    for statement in statements:
        connection.execute(sql.SQL(statement).format(app))


def install(dsn: str, settings: Settings, application_role: str = "memory_app", *, target_version=None, topic_template=None) -> bool:
    """Return True on first install, False on an unchanged repeat; never repair silently."""
    if not isinstance(settings, Settings):
        raise TypeError("settings must be validated Settings")
    target = LATEST_VERSION if target_version is None else target_version
    if type(target) is not int or target not in SUPPORTED_VERSIONS:
        raise ValueError("不支持的目标数据库版本")
    if topic_template not in (None, "lover", "general") or (topic_template is not None and target < 3):
        raise ValueError("主题模板必须是受支持版本的 lover 或 general")
    paths = {1: MIGRATION, **{v: MIGRATIONS / name for v, name in MIGRATION_FILES.items() if v <= target}}
    migrations = {v: path.read_text(encoding="utf-8") for v, path in paths.items()}
    checksums = {v: hashlib.sha256(text.encode()).hexdigest() for v, text in migrations.items()}
    with connect(dsn) as connection:
        # One transaction covers schema, grants, metadata and all functions.
        connection.execute("SELECT pg_advisory_xact_lock(7242261001)")
        version = int(connection.info.server_version / 10000)
        if version != 17:
            raise ValueError("本基线目前只支持已验证的 PostgreSQL 17")
        extension = connection.execute("""SELECT e.extversion,n.nspname FROM pg_extension e
            JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='vector'""").fetchone()
        if not extension or extension["nspname"] != "public" or not extension["extversion"].startswith("0.8."):
            raise ValueError("请由数据库管理员预先在 public 安装 pgvector 0.8.x 扩展")
        role = connection.execute("""SELECT oid,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls
            FROM pg_roles WHERE rolname=%s""", (application_role,)).fetchone()
        if not role or any(role[key] for key in ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")):
            raise ValueError("应用角色必须预先创建，且不能具有管理员权限")
        if connection.execute("SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=%s) AS value", (role["oid"],)).fetchone()["value"]:
            raise ValueError("应用角色不能继承其他角色")
        owner = connection.execute("SELECT current_user AS name").fetchone()["name"]
        if owner == application_role or connection.execute(
            "SELECT has_schema_privilege(%s,'public','CREATE') AS value", (application_role,)
        ).fetchone()["value"]:
            raise ValueError("应用角色不能是迁移所有者，也不能在 public 创建对象")

        current_version = 0
        namespace = connection.execute("SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname='memory_core'").fetchone()
        if namespace:
            if namespace["owner"] != owner:
                raise InstallationMismatch("已有 schema 的所有者不匹配；安装已停止")
            if not connection.execute("SELECT to_regclass('memory_core.installation') AS table_name").fetchone()["table_name"]:
                raise InstallationMismatch("已有 schema 缺少版本记录；不会覆盖现有对象")
            rows = connection.execute("SELECT * FROM memory_core.installation").fetchall()
            if len(rows) != 1 or rows[0]["version"] not in SUPPORTED_VERSIONS:
                raise InstallationMismatch("数据库版本不受支持；请使用相应版本的程序")
            record = rows[0]
            current_version = record["version"]
            if current_version > target:
                raise InstallationMismatch("不允许通过安装器降级数据库")
            if record["settings"] != settings.as_dict() or record["application_role"] != application_role:
                raise InstallationMismatch("安装配置与已有数据库不一致（维度、时区、业务日或角色）；不会修改已有数据")
            if current_version >= 3 and topic_template is not None and record["default_topic_template"] != topic_template:
                raise InstallationMismatch("安装主题模板与已有数据库不一致；不会重置已有主题")
            if record["migration_sha256"] != checksums[1] or record["catalog_sha256"] != catalog_fingerprint(connection):
                raise InstallationMismatch("迁移内容或数据库结构/权限不匹配；请检查后使用递增迁移")
            if current_version >= 2:
                history = connection.execute("SELECT version,migration_sha256 FROM memory_core.schema_migrations ORDER BY version").fetchall()
                expected = [{"version": v, "migration_sha256": checksums[v]} for v in range(1, current_version + 1)]
                if history != expected:
                    raise InstallationMismatch("已应用迁移的记录或文件校验和不匹配")

        if current_version == target:
            return False
        for version in range(current_version + 1, target + 1):
            migration = migrations[version].replace("__DIMENSIONS__", str(settings.dimensions))
            if version == 9:
                from .space import topic_presets
                migration = migration.replace("__TOPIC_PRESETS__", sql.Literal(Jsonb(topic_presets())).as_string(connection))
            try:
                connection.execute(migration)
                if version == DRAFT_POLICY_ALIGNMENT_VERSION:
                    _rebase_import_digests(connection)
            except psycopg.Error as exc:
                if version == 14 and exc.sqlstate == "P4001":
                    raise InstallationMismatch(
                        "存在不支持的旧材料来源；升级已回滚，原数据保留。请先导出或处理旧来源，再重试升级。"
                    ) from None
                raise
            timestamp = format_timestamp(datetime.now(timezone.utc), settings)
            if version == 1:
                connection.execute("""INSERT INTO memory_core.installation
                    (version,settings,application_role,migration_sha256,catalog_sha256,installed_at)
                    VALUES (1,%s,%s,%s,'',%s)""", (Jsonb(settings.as_dict()), application_role, checksums[1], timestamp))
            else:
                if version == 2:
                    connection.execute("""INSERT INTO memory_core.schema_migrations VALUES (1,%s,%s)""", (checksums[1], timestamp))
                connection.execute("INSERT INTO memory_core.schema_migrations VALUES (%s,%s,%s)", (version, checksums[version], timestamp))
                connection.execute("UPDATE memory_core.installation SET version=%s", (version,))
                if version == 3 and topic_template is not None:
                    connection.execute("UPDATE memory_core.installation SET default_topic_template=%s", (topic_template,))
            _grants(connection, application_role, version)
            connection.execute("UPDATE memory_core.installation SET catalog_sha256=%s", (catalog_fingerprint(connection),))
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the standalone memory storage baseline")
    defaults = Settings()
    parser.add_argument("--dimensions", type=int, default=defaults.dimensions)
    parser.add_argument("--timezone", default=defaults.timezone)
    parser.add_argument("--day-start-hour", type=int, default=defaults.day_start_hour)
    parser.add_argument("--application-role", default="memory_app")
    parser.add_argument("--target-version", type=int, choices=sorted(SUPPORTED_VERSIONS))
    parser.add_argument("--topic-template", choices=("lover", "general"))
    parser.add_argument("--dsn-env", default="MEMORY_INSTALL_DSN")
    args = parser.parse_args(argv)
    try:
        # Validate before reading credentials or connecting, including >2000 dimensions.
        settings = Settings(args.dimensions, args.timezone, args.day_start_hour)
        dsn = os.environ.get(args.dsn_env)
        if not dsn:
            raise ValueError("未设置指定的安装连接串环境变量")
        created = install(dsn, settings, args.application_role, target_version=args.target_version, topic_template=args.topic_template)
    except (ValueError, InstallationMismatch) as exc:
        parser.exit(2, f"安装停止：{exc}\n")
    print("安装完成" if created else "版本、配置与结构一致，保留已有数据")


if __name__ == "__main__":
    main()

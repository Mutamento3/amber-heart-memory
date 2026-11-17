"""Independent, owner-only migrations. Core-only installation needs no web tables."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from psycopg import sql
from memory_core.storage.db import connect
from memory_core.storage.install import InstallationMismatch
from memory_core.storage.schema import TOPIC_DETAILS_VERSION

MIGRATION = Path(__file__).with_name("migrations") / "001_admin_auth.sql"
MIGRATION_2 = MIGRATION.with_name("002_space_access_keys.sql")
MIGRATION_3 = MIGRATION.with_name("003_erase_revoked_key_ciphertext.sql")


def fingerprint(connection):
    queries = {
        "schema": "SELECT nspacl::text,pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='memory_web'",
        "tables": "SELECT c.relname,c.relkind,c.relacl::text,c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_web' ORDER BY c.relname",
        "columns": "SELECT c.relname,a.attname,a.attnum,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,a.attacl::text,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum WHERE n.nspname='memory_web' AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum",
        "constraints": "SELECT c.relname,x.conname,pg_get_constraintdef(x.oid),x.convalidated FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_web' ORDER BY c.relname,x.conname",
        "indexes": "SELECT c.relname,pg_get_indexdef(i.indexrelid),i.indisvalid,i.indisready FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_web' ORDER BY c.relname",
        "functions": "SELECT p.proname,pg_get_functiondef(p.oid),p.proacl::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='memory_web' ORDER BY p.oid::regprocedure::text",
        "triggers": "SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='memory_web' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname",
    }
    definitions = {
        name: connection.execute(query).fetchall() for name, query in queries.items()
    }
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


def install_web(dsn):
    migration = MIGRATION.read_text()
    checksum = hashlib.sha256(migration.encode()).hexdigest()
    migration_2 = MIGRATION_2.read_text()
    checksum_2 = hashlib.sha256(migration_2.encode()).hexdigest()
    migration_3 = MIGRATION_3.read_text()
    checksum_3 = hashlib.sha256(migration_3.encode()).hexdigest()
    with connect(dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(7242261002)")
        core = connection.execute(
            "SELECT version,application_role FROM memory_core.installation"
        ).fetchone()
        owner = connection.execute(
            "SELECT current_user=pg_get_userbyid(nspowner) AS owns FROM pg_namespace WHERE nspname='memory_core'"
        ).fetchone()
        if not owner or not owner["owns"] or core["version"] < TOPIC_DETAILS_VERSION:
            raise ValueError("先以配置所有者安装/升级 memory_core 到版本 7")
        previous = connection.execute(
            "SELECT obj_description(oid,'pg_namespace') AS record,pg_get_userbyid(nspowner)=current_user AS owns FROM pg_namespace WHERE nspname='memory_web'"
        ).fetchone()
        if previous:
            try:
                record = json.loads(previous["record"])
            except (ValueError, TypeError):
                raise InstallationMismatch("网页 schema 没有有效版本记录") from None
            expected = dict(version=record.get('version'), migration=checksum,
                catalog=fingerprint(connection), application_role=core['application_role'])
            if record.get('version') in (2, 3):
                expected['migration_2'] = checksum_2
            if record.get('version') == 3:
                expected['migration_3'] = checksum_3
            if not previous["owns"] or record.get('version') not in (1,2,3) or record != expected:
                raise InstallationMismatch(
                    "网页结构、权限或迁移不一致；不会覆盖已有对象"
                )
            if record['version'] == 3:
                return False
        else:
            connection.execute(migration)
        role = sql.Identifier(core["application_role"])
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA memory_web TO {}").format(role)
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT,INSERT,UPDATE,DELETE ON memory_web.admin_sessions,memory_web.admin_login_attempts TO {}"
            ).format(role)
        )
        if not previous or record['version'] == 1:
            connection.execute(migration_2)
        connection.execute(migration_3)
        # Ordinary workers may authenticate and show keys, but only the
        # configuration owner can issue or revoke them.
        connection.execute(sql.SQL('GRANT SELECT ON memory_web.space_access_keys TO {}').format(role))
        record = dict(
            version=3,
            migration=checksum,
            migration_2=checksum_2,
            migration_3=checksum_3,
            catalog=fingerprint(connection),
            application_role=core["application_role"],
        )
        connection.execute(
            sql.SQL("COMMENT ON SCHEMA memory_web IS {}").format(
                sql.Literal(json.dumps(record))
            )
        )
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        dsn = os.environ.get("MEMORY_INSTALL_DSN")
        if not dsn:
            raise ValueError("未设置 MEMORY_INSTALL_DSN")
        changed = install_web(dsn)
    except (ValueError, InstallationMismatch) as exc:
        parser.exit(2, f"安装停止：{exc}\n")
    print(
        "网页登录结构安装完成" if changed else "网页版本、结构与权限一致，保留现有会话"
    )


if __name__ == "__main__":
    main()

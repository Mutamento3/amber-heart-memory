"""Opaque sessions and a global login rate limit shared by all workers."""

from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from memory_core.db import connect
from .password import read_hash, verify_password

TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
BEIJING = timezone(timedelta(hours=8))


def timestamp(now=None):
    return (
        (now or datetime.now(BEIJING)).astimezone(BEIJING).isoformat(timespec="seconds")
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class AuthenticationError(ValueError):
    pass


class RateLimited(AuthenticationError):
    pass


class Sessions:
    def __init__(self, dsn, password_file):
        self.dsn, self.password_file = dsn, password_file
        read_hash(password_file)

    def login(self, password, remember, *, now=None):
        now = now or datetime.now(BEIJING)
        encoded = read_hash(self.password_file)
        with connect(self.dsn) as connection:
            # A DB lock makes the limit consistent even for simultaneous workers.
            connection.execute("SELECT pg_advisory_xact_lock(7242261003)")
            connection.execute(
                "DELETE FROM memory_web.admin_login_attempts WHERE expires_at<=%s",
                (timestamp(now),),
            )
            row = connection.execute(
                "SELECT count FROM memory_web.admin_login_attempts WHERE bucket='admin'"
            ).fetchone()
            if row and row["count"] >= 10:
                raise RateLimited("尝试过于频繁，请五分钟后再试")
            connection.execute(
                """INSERT INTO memory_web.admin_login_attempts VALUES ('admin',1,%s)
                ON CONFLICT(bucket) DO UPDATE SET count=admin_login_attempts.count+1""",
                (timestamp(now + timedelta(minutes=5)),),
            )
        if not verify_password(password, encoded):
            raise AuthenticationError("密码不正确")
        lifetime = 30 * 86400 if remember else 12 * 3600
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with connect(self.dsn) as connection:
            connection.execute(
                "DELETE FROM memory_web.admin_sessions WHERE expires_at<=%s OR password_version<>%s",
                (timestamp(now), digest(encoded)),
            )
            connection.execute(
                """DELETE FROM memory_web.admin_sessions WHERE digest IN
                (SELECT digest FROM memory_web.admin_sessions ORDER BY created_at DESC OFFSET 19)"""
            )
            connection.execute(
                "INSERT INTO memory_web.admin_sessions VALUES (%s,%s,%s,%s,%s)",
                (
                    digest(token),
                    csrf,
                    digest(encoded),
                    timestamp(now + timedelta(seconds=lifetime)),
                    timestamp(now),
                ),
            )
        return token, csrf, lifetime

    def lookup(self, token, *, now=None):
        if not TOKEN.fullmatch(token or ""):
            return None
        with connect(self.dsn) as connection:
            return connection.execute(
                """SELECT csrf,expires_at FROM memory_web.admin_sessions
                WHERE digest=%s AND password_version=%s AND expires_at>%s""",
                (digest(token), digest(read_hash(self.password_file)), timestamp(now)),
            ).fetchone()

    def revoke(self, token):
        with connect(self.dsn) as connection:
            connection.execute(
                "DELETE FROM memory_web.admin_sessions WHERE digest=%s",
                (digest(token),),
            )

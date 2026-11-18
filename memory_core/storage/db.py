"""Direct Postgres connections, always supplied explicitly by the caller."""

from contextlib import contextmanager
from threading import BoundedSemaphore

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

# The bundled server runs one process. PostgreSQL's 12 slots include three
# superuser reserves. Leave one ordinary slot free for maintenance and at
# least five of the service's slots for short queries when all scheduler
# session locks are held.
_background_slots = BoundedSemaphore(3)
_connection_slots = BoundedSemaphore(8)


@contextmanager
def connect(dsn: str):
    parameters = conninfo_to_dict(dsn)
    if parameters.get("service") or any(not parameters.get(key) for key in ("host", "dbname", "user")):
        raise ValueError("连接串必须明确指定 host、dbname、user，且不能使用 service 配置")
    with _connection_slots:
        connection = psycopg.connect(
            dsn, connect_timeout=5, row_factory=dict_row,
            options="-c search_path=pg_catalog,public,memory_core -c statement_timeout=15000",
        )
        try:
            with connection:
                yield connection
        finally:
            # Deferred constraints can fail during COMMIT itself.
            connection.close()


@contextmanager
def background_lock(dsn: str, key: str, *, wait_for_slot=False):
    """Try a long-lived scheduler lock without consuming the short-query reserve."""
    if not _background_slots.acquire(blocking=wait_for_slot):
        yield None
        return
    try:
        with connect(dsn) as connection:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked", (key,)
            ).fetchone()["locked"]
            connection.commit()
            # Closing the connection releases the session lock, including on error.
            yield connection if acquired else None
    finally:
        _background_slots.release()

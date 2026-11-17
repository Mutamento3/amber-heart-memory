"""Direct Postgres connections, always supplied explicitly by the caller."""

from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row


@contextmanager
def connect(dsn: str):
    parameters = conninfo_to_dict(dsn)
    if parameters.get("service") or any(not parameters.get(key) for key in ("host", "dbname", "user")):
        raise ValueError("连接串必须明确指定 host、dbname、user，且不能使用 service 配置")
    connection = psycopg.connect(
        dsn, connect_timeout=5, row_factory=dict_row,
        options="-c search_path=pg_catalog,public,memory_core -c statement_timeout=15000",
    )
    try:
        with connection:
            yield connection
    finally:
        # Deferred constraints can fail during COMMIT itself. Close even when
        # the driver's context-manager exit raises before closing its socket.
        connection.close()

"""One dedicated database per integration test, in the explicitly supplied test server."""

import os
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.admin = os.environ["MEMORY_TEST_ADMIN_DSN"]
        self.database = "memory_slice_" + uuid4().hex[:16]
        self.owner = make_conninfo(os.environ["MEMORY_TEST_OWNER_DSN"], dbname=self.database)
        self.app = make_conninfo(os.environ["MEMORY_TEST_APP_DSN"], dbname=self.database)
        with psycopg.connect(self.admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} OWNER memory_owner").format(sql.Identifier(self.database)))
        with psycopg.connect(make_conninfo(self.admin, dbname=self.database), autocommit=True) as connection:
            connection.execute("CREATE EXTENSION vector WITH SCHEMA public")
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(self.database)))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO memory_app").format(sql.Identifier(self.database)))

    def tearDown(self):
        with psycopg.connect(self.admin, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database)))

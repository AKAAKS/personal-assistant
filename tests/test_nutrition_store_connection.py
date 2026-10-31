import sqlite3
from nutrition_core.store.connection import DbConnection

class TestDbConnection:
    def test_sqlite3_connection_satisfies_protocol(self):
        conn = sqlite3.connect(":memory:")
        assert isinstance(conn, DbConnection)  # requires @runtime_checkable

    def test_object_missing_commit_does_not_satisfy_protocol(self):
        class NoCommit:
            def execute(self, sql, parameters=()):
                ...

            def executescript(self, sql):
                ...

        assert not isinstance(NoCommit(), DbConnection)

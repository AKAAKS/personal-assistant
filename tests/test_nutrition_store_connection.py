import sqlite3
from nutrition_core.store.connection import DbConnection

class TestDbConnection:
    def test_sqlite3_connection_satisfies_protocol(self):
        conn = sqlite3.connect(":memory:")
        assert isinstance(conn, DbConnection)  # requires @runtime_checkable

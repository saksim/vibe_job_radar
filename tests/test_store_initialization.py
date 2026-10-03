"""Actual SQLite initialization is durable, atomic and closed on failure."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_core import job
from vibe_job_radar.store import Store


class StoreInitializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'jobs.sqlite'
        self.connect = sqlite3.connect
        self.connections = []
        self.addCleanup(self.close_connections)

    def close_connections(self):
        for connection in self.connections:
            connection.close()

    @contextmanager
    def controlled_connections(self, *, denied=None, interrupt=False):
        def connect(*args, **kwargs):
            connection = self.connect(*args, **kwargs)
            self.connections.append(connection)
            if denied:
                connection.set_authorizer(lambda action, first, second, *_:
                    sqlite3.SQLITE_DENY if denied(action, first, second) else sqlite3.SQLITE_OK)
            if not interrupt:
                return connection
            class InterruptedSchema:
                # Interrupt either SQLite schema API before it returns to the
                # constructor, outside SQLite's callback exception handling.
                def __getattr__(self, name):
                    return getattr(connection, name)
                def execute(self, sql, *values):
                    if 'CREATE TABLE' in sql.upper():
                        raise KeyboardInterrupt()
                    return connection.execute(sql, *values)
                def executescript(self, sql):
                    if 'CREATE TABLE' in sql.upper():
                        raise KeyboardInterrupt()
                    return connection.executescript(sql)
            return InterruptedSchema()
        with patch('vibe_job_radar.store.sqlite3.connect', side_effect=connect):
            yield

    def schema(self):
        with closing(self.connect(self.path)) as connection:
            return (connection.execute('PRAGMA user_version').fetchone()[0],
                    connection.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall())

    def test_failed_new_schema_does_not_publish_partial_tables(self):
        with self.controlled_connections(denied=lambda action, first, second:
                action==sqlite3.SQLITE_CREATE_TABLE and first=='events'):
            with self.assertRaises(sqlite3.DatabaseError):
                Store(self.path)
        self.close_connections()  # Inspect persisted state through another connection.
        self.assertEqual(self.schema(), (0, []))

    def test_version_marker_failure_rolls_back_the_new_schema(self):
        with self.controlled_connections(denied=lambda action, first, second:
                action==sqlite3.SQLITE_PRAGMA and first=='user_version' and second is not None):
            with self.assertRaises(sqlite3.DatabaseError):
                Store(self.path)
        self.close_connections()
        self.assertEqual(self.schema(), (0, []))

    def test_sqlite_initialization_error_closes_the_connection(self):
        with self.controlled_connections(denied=lambda action, first, second:
                action==sqlite3.SQLITE_CREATE_TABLE and first=='events'):
            with self.assertRaises(sqlite3.DatabaseError):
                Store(self.path)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connections[-1].execute('SELECT 1')

    def test_keyboard_interrupt_also_closes_initialization_connection(self):
        with self.controlled_connections(interrupt=True):
            with self.assertRaises(KeyboardInterrupt):
                Store(self.path)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connections[-1].execute('SELECT 1')

    def test_schema_version_is_rechecked_after_another_initializer_wins(self):
        advanced = []
        def advance_version(action, first, second):
            if action==sqlite3.SQLITE_TRANSACTION and first=='BEGIN' and not advanced:
                # A second real connection commits between the first version
                # observation and acquisition of the initialization writer lock.
                with closing(self.connect(self.path)) as other:
                    other.execute('PRAGMA user_version=2')
                advanced.append(True)
            return False
        with self.controlled_connections(denied=advance_version):
            with self.assertRaisesRegex(ValueError, 'unsupported database schema: 2'):
                Store(self.path)
        self.assertEqual(advanced, [True])
        self.assertEqual(self.schema(), (2, []))

    def test_new_store_keeps_wal_full_durability_and_foreign_keys(self):
        record = job('使用 Cursor 完成代码审查。')
        with Store(self.path) as store:
            self.assertEqual(store.conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(store.conn.execute('PRAGMA synchronous').fetchone()[0], 2)
            self.assertEqual(store.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0], 1)
            store.add(record)
            with self.assertRaises(sqlite3.IntegrityError):
                store.conn.execute("INSERT INTO observations(record_id,collected_at,source_ref,source_mode) VALUES('missing','fixture','','synthetic')")
        with Store(self.path) as reopened:
            self.assertEqual(reopened.records(), [record])
            self.assertEqual(reopened.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()

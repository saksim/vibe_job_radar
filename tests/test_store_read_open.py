"""A current WAL store must not request a version write just to read a report."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_core import CONF, NOW, job
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class StoreReadOpenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'jobs.sqlite'
        self.record = job('必须使用 Cursor 进行代码审查。')
        with Store(self.path) as store:
            store.add(self.record)
            store.event('original', 'ok', {'authored': True})
        self.connect = sqlite3.connect
        self.opened = []
        self.addCleanup(self.close_opened)

    def close_opened(self):
        for connection in self.opened:
            connection.close()

    @contextmanager
    def bounded_connections(self, authorizer=None):
        # Bound only the artificial conflict. Production retains its 30s limit.
        def connect(*args, **kwargs):
            kwargs['timeout'] = .1
            connection = self.connect(*args, **kwargs)
            self.opened.append(connection)
            if authorizer is not None:
                connection.set_authorizer(authorizer)
            return connection
        with patch('vibe_job_radar.store.sqlite3.connect', side_effect=connect):
            yield

    @contextmanager
    def uncommitted_writer(self):
        with closing(self.connect(self.path)) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO events(created_at,action,status,details) VALUES('2000-01-01','uncommitted','fixture','{}')")
            try:
                yield writer
            finally:
                writer.rollback()

    def test_current_store_reads_committed_data_beside_an_uncommitted_writer(self):
        with self.uncommitted_writer(), self.bounded_connections():
            with Store(self.path) as reader:
                self.assertEqual(reader.records(), [self.record])
                self.assertEqual([r['action'] for r in reader.events()], ['original'])
                self.assertEqual(reader.conn.execute('PRAGMA user_version').fetchone()[0], 1)
        with Store(self.path) as store:
            store.event('later', 'ok', {})
            self.assertEqual([r['action'] for r in store.events()], ['original', 'later'])

    def test_current_schema_does_not_require_version_write_permission(self):
        denied = []
        def authorize(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_PRAGMA and first == 'user_version' and second is not None:
                denied.append((first, second))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        with self.bounded_connections(authorize), Store(self.path) as store:
            self.assertEqual(store.records(), [self.record])
        self.assertEqual(denied, [])

    def test_version_zero_upgrade_keeps_records_observations_and_events(self):
        with closing(self.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=0')
            connection.commit()
        with Store(self.path) as store:
            self.assertEqual(store.conn.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(store.records(latest_only=False), [self.record])
            self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
            self.assertEqual([r['action'] for r in store.events()], ['original'])

    def test_current_version_still_creates_missing_table_and_index(self):
        with closing(self.connect(self.path)) as connection:
            connection.executescript('DROP TABLE events; DROP INDEX records_identity;')
        with Store(self.path) as store:
            self.assertEqual(store.records(), [self.record])
            self.assertEqual(store.events(), [])
            self.assertEqual(store.conn.execute("SELECT type FROM sqlite_master WHERE name='records_identity'").fetchone(), ('index',))
            self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)

    def test_unknown_version_stays_rejected_without_downgrading(self):
        with closing(self.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=2')
            connection.commit()
        with self.assertRaisesRegex(ValueError, 'unsupported database schema: 2'):
            Store(self.path)
        with closing(self.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)

    def test_original_report_pipeline_can_read_during_a_writer_without_changing_old_report(self):
        analyze(self.path, self.root / 'old', config=CONF, as_of=NOW)
        before = {p.name:p.read_bytes() for p in (self.root / 'old').iterdir() if p.is_file()}
        with self.uncommitted_writer(), self.bounded_connections():
            result = analyze(self.path, self.root / 'new', config=CONF, as_of=NOW)
        self.assertEqual(result['stats']['stored_snapshots'], 1)
        self.assertEqual(result['stats']['full_text_job_groups'], 1)
        self.assertGreater(result['stats']['accepted_positive_requirement_rows'], 0)
        self.assertEqual(before, {p.name:p.read_bytes() for p in (self.root / 'old').iterdir() if p.is_file()})
        with Store(self.path) as store:
            self.assertEqual([r['action'] for r in store.events()], ['original'])

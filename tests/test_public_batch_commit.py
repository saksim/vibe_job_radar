"""Artificial public-source batches use the real persistent store and reports."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_local_public import payload, query
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.public_contract import as_record
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace


class PublicBatchCommitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Workspace(Path(temp.name) / 'workspace')
        transport = Mock()
        transport.json.return_value = payload(3)
        self.client = LocalPublicDataClient(self.workspace, transport=transport)
        self.tasks = PublicTasks(self.workspace, hybrid_client=self.client)
        self.addCleanup(self.tasks.close)
        self.query = query()
        self.result = self.client.search(self.query, consent=True)
        self.records = [as_record(job, self.client.registry[job['source']])
                        for job in self.result['response']['jobs']]
        self.assertEqual(len(self.records), 2)
        # Preserve an older observation and original history across failure.
        with Store(self.workspace.db) as store:
            store.add(self.records[0])
            store.event('prior-fixture', 'ok', {'retained': True})
        self.before = self.snapshot()

    def snapshot(self):
        with Store(self.workspace.db) as store:
            return {table: store.conn.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('records', 'observations', 'events')}

    def test_second_record_failure_rolls_back_first_observation_and_history(self):
        with Store(self.workspace.db) as store:
            store.conn.execute("CREATE TRIGGER reject_second BEFORE INSERT ON observations "
                               "WHEN NEW.record_id='" + self.records[1].record_id + "' "
                               "BEGIN SELECT RAISE(ABORT,'artificial second record failure'); END")
            store.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks._import(self.result, self.query)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(list((self.workspace.root / 'reports').iterdir()), [])

    def test_audit_failure_rolls_back_entire_public_batch(self):
        with Store(self.workspace.db) as store:
            store.conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON events "
                               "WHEN NEW.action='public_query_import' "
                               "BEGIN SELECT RAISE(ABORT,'artificial audit failure'); END")
            store.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.tasks._import(self.result, self.query)
        self.assertEqual(self.snapshot(), self.before)
        self.assertEqual(list((self.workspace.root / 'reports').iterdir()), [])

    def test_success_has_one_durable_transaction_and_retains_exact_report_and_observations(self):
        statements = []
        real_connect = sqlite3.connect
        def observed(path, *args, **kwargs):
            connection = real_connect(path, *args, **kwargs)
            if Path(path) == self.workspace.db:
                connection.set_trace_callback(statements.append)
            return connection
        with patch('vibe_job_radar.store.sqlite3.connect', side_effect=observed):
            result = self.tasks._import(self.result, self.query)
        commits = [sql for sql in statements if sql == 'COMMIT']
        self.assertEqual(len(commits), 1, statements)
        after = self.snapshot()
        self.assertEqual(len(after['records']), 2)
        self.assertEqual(len(after['observations']), 3)
        self.assertEqual(after['events'][0], self.before['events'][0])
        event = after['events'][1]
        self.assertEqual(event[2:4], ('public_query_import', 'ok'))
        self.assertEqual(json.loads(event[4]), {'source_scope': list(self.query.source_scope),
                         'execution_mode': 'local_direct', 'observations': 2, 'new_snapshots': 1})
        report = self.workspace.root / 'reports' / result['report_id']
        exported = [json.loads(line) for line in (report / 'jobs.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual({row['record_id'] for row in exported}, {row.record_id for row in self.records})
        self.assertEqual({row['text'] for row in exported}, {row.text for row in self.records})
        self.assertTrue((report / 'public_source.json').is_file())


if __name__ == '__main__':
    unittest.main()

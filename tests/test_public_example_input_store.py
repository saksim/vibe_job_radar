"""Public examples retain durable outputs without a disposable disk database."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_redirect_recovery import fixture_payload
from vibe_job_radar import public_example
from vibe_job_radar.public_example import API_URL, PublicExample
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace


class PublicExampleInputStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Workspace(Path(temporary.name) / 'workspace')
        self.transport = Mock()
        self.transport.json.return_value = fixture_payload()
        self.example = PublicExample(self.workspace, self.transport)

    def test_unavailable_disposable_database_keeps_full_report_and_durable_audits(self):
        def open_store(path, **kwargs):
            if str(path) != ':memory:' and Path(path) != self.workspace.db:
                raise PermissionError('fixture disposable database unavailable')
            return Store(path, **kwargs)

        with patch.object(public_example, 'Store', side_effect=open_store), \
                patch.object(os, 'fsync', wraps=os.fsync) as flushed:
            try:
                result = self.example.run({'consent': True})
            except PermissionError as error:
                self.fail('durable source was unnecessarily copied to a disposable database: ' + str(error))
        self.assertTrue(result['success'])
        self.transport.json.assert_called_once_with(API_URL)
        with Store(self.workspace.db) as stored:
            records = stored.records()
        self.assertEqual(len(records), 1)
        report = self.workspace.root / 'reports' / result['report_id']
        manifest = json.loads((report / 'run_manifest.json').read_text(encoding='utf-8'))
        hashes = manifest['output_files_sha256']
        self.assertEqual(len(hashes), 30)  # All 29 original exports plus source provenance.
        self.assertEqual({p.name for p in report.iterdir()}, {*hashes, 'run_manifest.json'})
        for name, digest in hashes.items():
            self.assertEqual(hashlib.sha256((report / name).read_bytes()).hexdigest(), digest)
        jobs = [json.loads(line) for line in (report / 'jobs.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(jobs, [records[0].to_dict()])
        previous = self.example.root / (result['id'] + '.json')
        latest = self.example.root / 'latest.json'
        self.assertEqual(previous.read_bytes(), latest.read_bytes())
        self.assertEqual(json.loads(latest.read_text(encoding='utf-8'))['report_id'], result['report_id'])
        self.assertEqual(flushed.call_count, len(hashes) + 3)  # Manifest and both audit records.
        self.assertEqual(list(self.example.root.glob('.public-example-*')), [])
        cached = self.example.run({'consent': True})
        self.assertTrue(cached['cache_reused'])
        self.assertEqual(cached['report_id'], result['report_id'])
        self.transport.json.assert_called_once_with(API_URL)

    def test_report_failure_closes_batch_and_preserves_durable_source(self):
        failure = OSError('fixture report write unavailable')
        batches = []

        def fail_report(batch, *args, **kwargs):
            self.assertIsInstance(batch, Store)
            self.assertEqual(batch.conn.execute('PRAGMA database_list').fetchall(), [(0, 'main', '')])
            batches.append(batch)
            raise failure

        with patch('vibe_job_radar.pipeline.analyze', side_effect=fail_report):
            with self.assertRaises(OSError) as raised:
                self.example.run({'consent': True})
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(batches), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            batches[0].conn.execute('SELECT 1')
        with Store(self.workspace.db) as stored:
            self.assertEqual(len(stored.records()), 1)
        self.assertEqual(list((self.workspace.root / 'reports').iterdir()), [])
        self.assertFalse((self.example.root / 'latest.json').exists())
        self.assertEqual(list(self.example.root.glob('*.json')), [])
        self.assertEqual(list(self.example.root.glob('.public-example-*')), [])


if __name__ == '__main__':
    unittest.main()

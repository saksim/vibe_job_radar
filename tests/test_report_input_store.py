"""Borrowed batch databases preserve SQL snapshot semantics and report durability."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_core import NOW, job
from vibe_job_radar import pipeline
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class ReportInputStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def populate(self, store):
        # Late backfill and multiple source identities exercise the Store's
        # ordering/selection, rather than replacing it with a list shortcut.
        store.add(job())
        store.add(job(text='旧版岗位正文。', collected_at='2026-08-01T00:00:00+00:00'))
        store.add(job(url='https://www.liepin.com/job/fixture.shtml', platform='liepin'))
        store.event('fixture', 'ok', {'synthetic_test': True})

    def test_memory_and_file_input_produce_all_thirty_identical_files(self):
        path = self.root / 'records.sqlite'
        with patch('vibe_job_radar.store.utc_now', return_value=NOW), Store(path) as stored:
            self.populate(stored)
        with patch('vibe_job_radar.store.utc_now', return_value=NOW), Store(':memory:') as batch:
            self.populate(batch)
            before = batch.records(latest_only=False)
            self.assertEqual(batch.conn.execute('PRAGMA database_list').fetchall(), [(0, 'main', '')])
            with patch('vibe_job_radar.pipeline.utc_now', return_value=NOW):
                first = analyze(path, self.root / 'disk', as_of=NOW)
                second = analyze(batch, self.root / 'memory', as_of=NOW)
            self.assertEqual(first, second)
            self.assertEqual(first['stats']['stored_snapshots'], 3)
            self.assertEqual(first['stats']['current_source_records'], 2)
            files = {p.name: p.read_bytes() for p in (self.root / 'disk').iterdir()}
            self.assertEqual(len(files), 30)
            self.assertEqual(files, {p.name: p.read_bytes() for p in (self.root / 'memory').iterdir()})
            self.assertEqual(batch.records(latest_only=False), before)
            batch.event('after_analysis', 'ok', {})
            self.assertEqual(len(batch.events()), 2)

    def test_read_failure_does_not_close_or_replace_borrowed_store(self):
        with Store(':memory:') as batch:
            self.populate(batch)
            with patch.object(batch, 'records', side_effect=sqlite3.OperationalError('fixture read failure')):
                with self.assertRaises(sqlite3.OperationalError):
                    analyze(batch, self.root / 'report', as_of=NOW)
            self.assertEqual(len(batch.records(latest_only=False)), 3)
            self.assertFalse((self.root / 'report').exists())

    def test_export_failure_keeps_input_and_cleans_unpublished_report(self):
        original = pipeline.atomic_text
        def fail_job_export(path, text):
            if path.name == 'jobs.jsonl':
                raise OSError('fixture export failure')
            return original(path, text)
        with Store(':memory:') as batch:
            self.populate(batch)
            before = batch.records(latest_only=False)
            with patch.object(pipeline, 'atomic_text', side_effect=fail_job_export):
                with self.assertRaisesRegex(OSError, 'fixture export failure'):
                    analyze(batch, self.root / 'report', as_of=NOW)
            self.assertEqual(batch.records(latest_only=False), before)
            self.assertFalse((self.root / 'report').exists())
            self.assertEqual(list(self.root.glob('.report-*')), [])

    def test_missing_database_path_is_still_rejected_without_creation(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaisesRegex(ValueError, 'database does not exist'):
            analyze(missing, self.root / 'report', as_of=NOW)
        self.assertFalse(missing.exists())
        self.assertFalse((self.root / 'report').exists())

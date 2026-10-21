"""Artificial local data only; faults never count as human JD adjudication."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_core import CONF, NOW, job
import test_quality_ui as quality_fixtures
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.quality_ui import QualityService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace


class QualityHistoryProtectionTests(unittest.TestCase):
    setUp = quality_fixtures.QualityUITests.setUp
    open = quality_fixtures.QualityUITests.open
    form = quality_fixtures.QualityUITests.form
    assert_original_unchanged = quality_fixtures.QualityUITests.assert_original_unchanged

    def test_lost_database_cannot_replace_saved_annotations_with_blank_revision(self):
        self.service.save(self.form(self.open()))
        database = self.service.root / 'history.sqlite'
        packets = {p.relative_to(self.service.root): p.read_bytes()
                   for p in self.service.root.rglob('*') if p.is_file() and p != database}
        database.unlink()
        for service in (self.service, QualityService(self.workspace)):
            with self.assertRaises(InputError):
                service.open({'report_id': self.run})
            self.assertFalse(database.exists())
        self.assertEqual(packets, {p.relative_to(self.service.root): p.read_bytes()
                         for p in self.service.root.rglob('*') if p.is_file()})
        self.assert_original_unchanged()

    def test_preexisting_empty_history_directory_is_not_assumed_new(self):
        self.service.root.mkdir()
        with self.assertRaises(InputError):
            self.open()
        self.assertEqual(list(self.service.root.iterdir()), [])

    def test_normal_sqlite_analyze_preserves_labels_and_database_bytes(self):
        saved = self.service.save(self.form(self.open()))
        database = self.service.root / 'history.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.execute('ANALYZE')
            connection.commit()
        before = database.read_bytes()
        reopened = self.open()
        self.assertEqual(reopened['revision'], saved['revision'])
        self.assertEqual(reopened['annotations'], saved['annotations'])
        self.assertEqual(database.read_bytes(), before)
        self.assert_original_unchanged()

    def test_initial_connection_failure_does_not_leave_a_broken_history_root(self):
        with patch('vibe_job_radar.quality_ui.sqlite3.connect', side_effect=OSError('artificial first launch')):
            with self.assertRaises(OSError):
                self.open()
        self.assertFalse(self.service.root.exists())
        self.assertEqual(self.open()['revision'], 0)

    def test_existing_wrong_columns_are_rejected_without_changes(self):
        self.open()
        database = self.service.root / 'history.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript('DROP TABLE packets; CREATE TABLE packets(report_id TEXT PRIMARY KEY);')
        before = database.read_bytes()
        with self.assertRaises(InputError):
            self.open()
        self.assertEqual(database.read_bytes(), before)

    def test_initial_publication_failure_can_retry_without_losing_history(self):
        with patch('vibe_job_radar.quality_ui.os.rename', side_effect=OSError('artificial publish failure')):
            with self.assertRaises(OSError):
                self.open()
        self.assertFalse(self.service.root.exists())
        self.assertEqual(list(self.workspace.root.glob('.quality-init-*')), [])
        self.assertEqual(self.open()['revision'], 0)

    def test_competing_initial_publication_keeps_the_winner_annotations(self):
        original = os.rename
        armed = True
        winner = None
        def interleave(source, target, *args, **kwargs):
            nonlocal armed, winner
            if armed and Path(target) == self.service.root:
                armed = False
                other = QualityService(self.workspace)
                winner = other.save(self.form(other.open({'report_id': self.run})))
            return original(source, target, *args, **kwargs)
        with patch('vibe_job_radar.quality_ui.os.rename', side_effect=interleave):
            reopened = self.open()
        self.assertFalse(armed)
        self.assertEqual(reopened['revision'], 1)
        self.assertEqual(reopened['annotations'], winner['annotations'])
        self.assertEqual(list(self.workspace.root.glob('.quality-init-*')), [])
        self.assert_original_unchanged()


class SourceReadProtectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = Workspace(self.root / 'workspace')
        self.path = self.workspace.db
        self.record = job(is_synthetic=True, source_mode='synthetic')
        with Store(self.path) as store:
            store.add(self.record)
            store.event('artificial fixture', 'ok', {})

    def damage(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('DROP TABLE records')
            connection.commit()
        return self.path.read_bytes()

    def test_report_cannot_publish_empty_success_from_a_missing_records_table(self):
        before = self.damage()
        output = self.root / 'report'
        with self.assertRaises(ValueError):
            analyze(self.path, output, config=CONF, as_of=NOW, demo_mode=True)
        self.assertFalse(output.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_workbench_status_cannot_repair_a_missing_source_table(self):
        before = self.damage()
        with self.assertRaises(InputError):
            self.workspace.status()
        self.assertEqual(self.path.read_bytes(), before)

    def test_analysis_does_not_upgrade_a_complete_legacy_source_database(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute('PRAGMA user_version=0')
            connection.commit()
        before = self.path.read_bytes()
        result = analyze(self.path, self.root / 'report', config=CONF, as_of=NOW, demo_mode=True)
        self.assertEqual(result['stats']['stored_snapshots'], 1)
        self.assertEqual(self.path.read_bytes(), before)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)

    def test_read_only_connection_refuses_writes_and_keeps_existing_observations(self):
        before = self.path.read_bytes()
        with Store(self.path, read_only=True) as store:
            self.assertEqual(store.records(), [self.record])
            with self.assertRaises(sqlite3.OperationalError):
                store.event('must not persist', 'fixture', {})
        self.assertEqual(self.path.read_bytes(), before)
        with Store(self.path) as writable:
            self.assertEqual([row['action'] for row in writable.events()], ['artificial fixture'])
            writable.event('explicit write preserved', 'ok', {})
            self.assertEqual(len(writable.events()), 2)


if __name__ == '__main__':
    unittest.main()

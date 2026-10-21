"""Read-only source directories and committed WAL data use artificial fixtures."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import shutil

from test_core import CONF, NOW, job
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.path = self.source / 'jobs.sqlite'
        self.first = job()
        with Store(self.path) as writer:
            writer.add(self.first)
        self.temp_root = self.root / 'temporary'
        self.temp_root.mkdir()

    def files(self, *, include_shm=True):
        return {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file() and (include_shm or not p.name.endswith('-shm'))}

    def test_quiescent_read_only_directory_has_no_new_sidecars(self):
        before = self.files()
        self.assertEqual(set(before), {'jobs.sqlite'})
        self.path.chmod(0o400)
        self.source.chmod(0o500)
        try:
            result = analyze(self.path, self.root / 'report', config=CONF, as_of=NOW)
            self.assertEqual(result['stats']['stored_snapshots'], 1)
            self.assertEqual(self.files(), before)
        finally:
            self.source.chmod(0o700)
            self.path.chmod(0o600)

    def test_committed_wal_is_read_while_writer_has_uncommitted_changes(self):
        second = job(url='https://www.liepin.com/job/artificial-second.shtml', platform='liepin')
        with Store(self.path) as writer:
            writer.conn.execute('PRAGMA wal_autocheckpoint=0')
            writer.add(second)
            writer.conn.execute('BEGIN IMMEDIATE')
            writer.conn.execute("INSERT INTO events(created_at,action,status,details) VALUES('2000-01-01','uncommitted','fixture','{}')")
            # Windows locks SHM coordination bytes while the writer is active.
            before = self.files(include_shm=False)
            self.assertIn('jobs.sqlite-wal', before)
            try:
                with Store(self.path, read_only=True) as reader:
                    self.assertEqual({j.record_id for j in reader.records()}, {self.first.record_id, second.record_id})
                    self.assertEqual(reader.events(), [])
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.event('forbidden', 'fixture', {})
                self.assertEqual(self.files(include_shm=False), before)
            finally:
                writer.conn.rollback()

    def test_change_during_copy_is_retried_and_new_commit_is_preserved(self):
        original = shutil.copyfile
        armed = True
        second = job(url='https://www.liepin.com/job/artificial-after-copy.shtml', platform='liepin')
        with Store(self.path) as writer:
            writer.event('initial WAL', 'fixture', {})
            def interleave(source, target, *args, **kwargs):
                nonlocal armed
                result = original(source, target, *args, **kwargs)
                if armed and Path(source).name.endswith('-wal'):
                    armed = False
                    writer.add(second)
                return result
            with patch('vibe_job_radar.store.shutil.copyfile', side_effect=interleave):
                with Store(self.path, read_only=True) as reader:
                    self.assertEqual({j.record_id for j in reader.records()}, {self.first.record_id, second.record_id})
            self.assertFalse(armed)

    def test_continuously_changing_source_fails_without_report_or_temp_residue(self):
        original = shutil.copyfile
        changes = []
        with Store(self.path) as writer:
            writer.event('initial WAL', 'fixture', {})
            def interleave(source, target, *args, **kwargs):
                result = original(source, target, *args, **kwargs)
                if Path(source).name.endswith('-wal'):
                    changes.append(len(changes))
                    writer.event('concurrent '+str(len(changes)), 'fixture', {})
                return result
            with patch('vibe_job_radar.store.shutil.copyfile', side_effect=interleave), \
                 patch('tempfile.tempdir', str(self.temp_root)):
                with self.assertRaises(sqlite3.OperationalError):
                    analyze(self.path, self.root / 'report', config=CONF, as_of=NOW)
            self.assertEqual(len(changes), 3)
            self.assertFalse((self.root / 'report').exists())
            self.assertEqual(list(self.temp_root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()



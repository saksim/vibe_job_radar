"""Offline failure injection; no personal materials or live platform requests."""
import base64
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.evidence_ui import EvidenceService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace
from test_workbench import capture


class LocalWriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Workspace(temp.name)
        self.service = EvidenceService(self.workspace)

    @staticmethod
    def request(raw=b'artificial complete attachment'):
        return {'name': 'proof.txt', 'content_base64': base64.b64encode(raw).decode(),
                'rights_confirmed': True}

    def artifacts(self):
        with closing(self.service.connect()) as conn:
            return conn.execute('SELECT * FROM artifacts ORDER BY id').fetchall()

    def test_interrupted_attachment_write_is_not_published_and_can_retry(self):
        original = Path.open
        root = self.service.artifacts

        class Interrupted:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def write(self, raw):
                self.stream.write(raw[:3])
                raise OSError('artificial full disk')

        def open_file(path, mode='r', *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            if mode == 'xb' and root in path.parents:
                return Interrupted(stream)
            return stream

        with patch.object(Path, 'open', open_file):
            with self.assertRaises((InputError, OSError)):
                self.service.upload(self.request())
        self.assertEqual(self.artifacts(), [])
        self.assertEqual(list(root.iterdir()), [], 'a partial canonical attachment blocks retries')
        uploaded = self.service.upload(self.request())
        self.assertEqual(self.service._artifact_path(uploaded['artifact_id']).read_bytes(),
                         b'artificial complete attachment')

    def test_attachment_flush_failure_does_not_register_or_publish(self):
        with patch('vibe_job_radar.evidence_ui.os.fsync', side_effect=OSError('artificial flush failure')):
            with self.assertRaises(InputError):
                self.service.upload(self.request())
        self.assertEqual(self.artifacts(), [])
        self.assertEqual(list(self.service.artifacts.iterdir()), [])
        self.assertEqual(self.service.upload(self.request())['size'], 30)

    def test_attachment_database_failure_is_actionable_and_complete_file_can_retry(self):
        with closing(self.service.connect()) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_artifact BEFORE INSERT ON artifacts
                BEGIN SELECT RAISE(ABORT, 'artificial metadata failure'); END""")
        try:
            self.service.upload(self.request())
        except (InputError, sqlite3.Error) as error:
            failure = error
        else:
            self.fail('database fault was not exercised')
        self.assertEqual(self.artifacts(), [])
        self.assertIsInstance(failure, InputError)
        with closing(self.service.connect()) as conn, conn:
            conn.execute('DROP TRIGGER reject_artifact')
        first = self.service.upload(self.request())
        second = self.service.upload(self.request())
        self.assertEqual(first, second)
        self.assertEqual(len(self.artifacts()), 1)
        self.assertEqual(self.service._artifact_path(first['artifact_id']).read_bytes(),
                         b'artificial complete attachment')

    def test_competing_attachment_uploads_cannot_both_take_last_capacity_slot(self):
        with closing(self.service.connect()) as conn, conn:
            for index in range(99):
                raw = ('existing fixture ' + str(index)).encode()
                ident = hashlib.sha256(raw).hexdigest()
                (self.service.artifacts / ident).write_bytes(raw)
                conn.execute('INSERT INTO artifacts VALUES(?,?,?,?)',
                             (ident, 'existing.txt', len(raw), 'fixture'))
        original_connect = self.service.connect
        original_open = Path.open
        attempted = []
        competing = []

        def connect():
            conn = original_connect()
            # A deterministic competing connection, without sleeping or waiting
            # for the normal 30-second SQLite busy deadline.
            conn.execute('PRAGMA busy_timeout=0')
            return conn

        def open_file(path, mode='r', *args, **kwargs):
            if mode == 'xb' and self.service.artifacts in path.parents and not attempted:
                attempted.append(True)
                try:
                    competing.append(self.service.upload(self.request(b'second fixture')))
                except InputError:
                    competing.append('busy')
            return original_open(path, mode, *args, **kwargs)

        with patch.object(self.service, 'connect', connect), patch.object(Path, 'open', open_file):
            self.service.upload(self.request())
        self.assertEqual(attempted, [True])
        self.assertEqual(competing, ['busy'])
        self.assertEqual(len(self.artifacts()), 100)
        with self.assertRaisesRegex(InputError, '50 MB / 100'):
            self.service.upload(self.request(b'second fixture'))
        self.assertEqual(len(list(self.service.artifacts.iterdir())), 100)

    def test_existing_attachment_tampering_is_not_repaired_or_overwritten(self):
        uploaded = self.service.upload(self.request())
        path = self.service.artifacts / uploaded['artifact_id']
        before = self.artifacts()
        path.write_bytes(b'changed original')
        with self.assertRaises(InputError):
            self.service.upload(self.request())
        self.assertEqual(path.read_bytes(), b'changed original')
        self.assertEqual(self.artifacts(), before)

    def records(self):
        with closing(sqlite3.connect(self.workspace.db)) as conn:
            return {table: conn.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('records', 'observations', 'events')}

    def test_manual_capture_event_failure_rolls_back_and_retry_preserves_counts(self):
        self.workspace.add_job(capture())
        with closing(sqlite3.connect(self.workspace.db)) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_capture BEFORE INSERT ON events
                WHEN NEW.action='workbench_capture'
                BEGIN SELECT RAISE(ABORT, 'artificial capture audit failure'); END""")
        before = self.records()
        for data in (capture(), capture(url='https://www.zhipin.com/job_detail/new-fixture.html')):
            with self.subTest(existing=data['url'].endswith('test-fixture.html')):
                try:
                    self.workspace.add_job(data)
                except (InputError, sqlite3.Error) as error:
                    failure = error
                else:
                    self.fail('capture fault was not exercised')
                self.assertEqual(self.records(), before)
                self.assertIsInstance(failure, InputError)
        with closing(sqlite3.connect(self.workspace.db)) as conn, conn:
            conn.execute('DROP TRIGGER reject_capture')
        self.assertEqual(self.workspace.add_job(capture())['new_snapshots'], 0)
        self.assertEqual(self.workspace.add_job(capture(url='https://www.zhipin.com/job_detail/new-fixture.html'))['new_snapshots'], 1)
        state = self.records()
        self.assertEqual([len(state[t]) for t in ('records', 'observations', 'events')], [2, 3, 3])

    def test_manual_capture_observation_failure_returns_recoverable_ui_error(self):
        with Store(self.workspace.db):
            pass
        with closing(sqlite3.connect(self.workspace.db)) as conn, conn:
            conn.execute("""CREATE TRIGGER reject_observation BEFORE INSERT ON observations
                BEGIN SELECT RAISE(ABORT, 'artificial observation failure'); END""")
        before = self.records()
        with self.assertRaises(InputError):
            self.workspace.add_job(capture())
        self.assertEqual(self.records(), before)

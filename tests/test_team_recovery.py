"""Whole-service recovery against private local fixtures, without deployment."""
import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar.team_service import initialize_service, main, TeamApplication
from vibe_job_radar.team_store import TeamError, TeamStore
from vibe_job_radar.team_catalog import TeamCatalog
from vibe_job_radar.team_recovery import verify_backup, restore_backup, MANIFEST
from vibe_job_radar.guided.rate import RateLedger, RateLimit


class TeamRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='team-recovery-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = self.root / 'service'
        self.backup = self.root / 'backup'
        initialize_service(self.service)
        self.store = TeamStore(self.service)
        self.store.member('group', 'alice', 'collector')
        self.token = self.store.issue('group', 'alice', 'device')
        self.ledger = RateLedger(self.service / 'rates.sqlite')
        self.ledger.cool('fixture', 300)

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err):
            status = main(list(args))
        return status, out.getvalue(), err.getvalue()

    def create_backup(self):
        status, _, error = self.command('--data', str(self.service), 'backup', '--output', str(self.backup))
        self.assertEqual(status, 0, error)
        return verify_backup(self.backup)

    def fingerprint(self, root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def test_verify_is_readonly_and_cli_does_not_create_data_directory(self):
        proof = self.create_backup()
        before = self.fingerprint(self.backup)
        target = self.root / 'unused'
        status, output, error = self.command('--data', str(target), 'verify-backup', '--backup', str(self.backup))
        self.assertEqual(status, 0, error)
        self.assertNotIn('files', json.loads(output))
        self.assertEqual(json.loads(output)['file_count'], 3)
        self.assertEqual(before, self.fingerprint(self.backup))
        self.assertFalse(target.exists())
        self.assertEqual(proof['file_count'], 3)

    def test_restore_preserves_whole_state_and_original_backup(self):
        proof = self.create_backup()
        before = self.fingerprint(self.backup)
        target = self.root / 'restored'
        self.assertEqual(restore_backup(self.backup, target, confirm_quiescent=True)['file_count'], 3)
        for name in proof['files']:
            self.assertEqual((target/name).read_bytes(), (self.backup/name).read_bytes())
        self.assertEqual(before, self.fingerprint(self.backup))
        with self.assertRaises(RateLimit):
            RateLedger(target/'rates.sqlite').reserve('fixture', 'request')
        app = TeamApplication(target, host='recovery.fixture.test')
        self.assertEqual(app.store.state(self.token)['role'], 'collector')
        self.assertFalse((target/'.restore-in-progress').exists())

    def test_confirmation_and_existing_target_rejected_without_changes(self):
        self.create_backup()
        target = self.root/'target'
        with self.assertRaisesRegex(TeamError, 'confirmation_required'):
            restore_backup(self.backup, target)
        self.assertFalse(target.exists())
        target.mkdir()
        sentinel = target/'private.txt'
        sentinel.write_text('keep')
        with self.assertRaisesRegex(TeamError, 'team_output_exists'):
            restore_backup(self.backup, target, confirm_quiescent=True)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_corrupt_digest_and_missing_file_do_not_create_target(self):
        self.create_backup()
        original = (self.backup/'rates.sqlite').read_bytes()
        for corruption in ('changed', 'missing'):
            with self.subTest(corruption=corruption):
                if corruption == 'changed':
                    (self.backup/'rates.sqlite').write_bytes(original+b'changed')
                else:
                    (self.backup/'rates.sqlite').unlink()
                with self.assertRaises(TeamError):
                    restore_backup(self.backup, self.root/'target', confirm_quiescent=True)
                self.assertFalse((self.root/'target').exists())
                (self.backup/'rates.sqlite').write_bytes(original)

    def test_future_schema_rejected_even_with_matching_digest(self):
        self.create_backup()
        path = self.backup/'control.sqlite'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('PRAGMA user_version=99')
        manifest = json.loads((self.backup/MANIFEST).read_text())
        manifest['files']['control.sqlite']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest['files']['control.sqlite']['bytes'] = path.stat().st_size
        (self.backup/MANIFEST).write_text(json.dumps(manifest))
        with self.assertRaisesRegex(TeamError, 'database_invalid'):
            verify_backup(self.backup)

    def test_unknown_files_and_legacy_backups_are_not_silently_restored(self):
        self.create_backup()
        (self.backup/'private-extra.txt').write_text('not part of service')
        with self.assertRaisesRegex(TeamError, 'unexpected_file'):
            verify_backup(self.backup)
        (self.backup/'private-extra.txt').unlink()
        (self.backup/MANIFEST).unlink()
        with self.assertRaisesRegex(TeamError, 'manifest_required'):
            restore_backup(self.backup, self.root/'target', confirm_quiescent=True)
        self.assertFalse((self.root/'target').exists())

    def test_copy_failure_keeps_backup_and_blocks_partial_destination(self):
        self.create_backup()
        before = self.fingerprint(self.backup)
        target = self.root/'target'
        with patch('vibe_job_radar.team_recovery.shutil.copyfileobj', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                restore_backup(self.backup, target, confirm_quiescent=True)
        self.assertEqual(before, self.fingerprint(self.backup))
        for action in (lambda: initialize_service(target),
                       lambda: TeamApplication(target, host='recovery.fixture.test'),
                       lambda: TeamStore(target).member('group', 'other', 'admin')):
            with self.assertRaisesRegex(TeamError, 'team_restore_incomplete'):
                action()

    def test_refresh_ledger_and_cache_are_copied_together(self):
        cache = self.service/'refresh-workspace/local_public'
        cache.mkdir(parents=True)
        (cache/'cursor.key').write_bytes(b'opaque-fixture')
        rates = self.service/'refresh-workspace/public_examples/rates.sqlite'
        RateLedger(rates).cool('fixture', 300)
        self.create_backup()
        target = self.root/'target'
        restore_backup(self.backup, target, confirm_quiescent=True)
        self.assertEqual((target/'refresh-workspace/local_public/cursor.key').read_bytes(), b'opaque-fixture')
        with self.assertRaises(RateLimit):
            RateLedger(target/'refresh-workspace/public_examples/rates.sqlite').reserve('fixture', 'request')

    def test_refresh_cache_without_ledger_can_be_archived_but_not_restored(self):
        cache = self.service/'refresh-workspace/local_public'
        cache.mkdir(parents=True)
        (cache/'cursor.key').write_bytes(b'opaque-fixture')
        self.create_backup()
        with self.assertRaisesRegex(TeamError, 'refresh_ledger_missing'):
            restore_backup(self.backup, self.root/'target', confirm_quiescent=True)
        self.assertFalse((self.root/'target').exists())

    def test_manifest_traversal_and_duplicate_keys_are_refused(self):
        self.create_backup()
        path = self.backup/MANIFEST
        original = path.read_bytes()
        manifest = json.loads(original)
        manifest['files']['../outside'] = next(iter(manifest['files'].values()))
        path.write_text(json.dumps(manifest))
        with self.assertRaises(TeamError):
            restore_backup(self.backup, self.root/'target', confirm_quiescent=True)
        self.assertFalse((self.root/'target').exists())
        path.write_bytes(original.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1'))
        with self.assertRaises(TeamError):
            verify_backup(self.backup)

    def test_mid_publication_failure_retains_marker_and_original_backup(self):
        self.create_backup()
        original = self.fingerprint(self.backup)
        target = self.root/'target'
        rename = Path.rename
        moved = []
        def fail_second(path, destination):
            if path.parent.name.startswith('.restore-'):
                if moved:
                    raise OSError('publication interrupted')
                moved.append(path.name)
            return rename(path, destination)
        with patch.object(Path, 'rename', fail_second):
            with self.assertRaises(OSError):
                restore_backup(self.backup, target, confirm_quiescent=True)
        self.assertEqual(len(moved), 1)
        self.assertTrue((target/moved[0]).exists())
        with self.assertRaisesRegex(TeamError, 'team_restore_incomplete'):
            TeamApplication(target, host='recovery.fixture.test')
        self.assertEqual(original, self.fingerprint(self.backup))
        restore_backup(self.backup, self.root/'retry', confirm_quiescent=True)

    def test_verify_cli_does_not_need_an_unrelated_data_path(self):
        self.create_backup()
        status, output, error = self.command('verify-backup', '--backup', str(self.backup))
        self.assertEqual(status, 0, error)
        self.assertTrue(json.loads(output)['ok'])

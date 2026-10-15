"""Bounded artificial artifacts exercise rerun identity and extraction, without GitHub."""
import copy
import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/release_artifacts.py'


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('release_artifact_fixture', SCRIPT)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.selected = dict(id=123, run_attempt=2, head_sha='a' * 40, status='completed',
            conclusion='success', run_started_at='2026-10-15T22:13:51Z', updated_at='2026-10-15T22:36:00Z')
        self.names = ('package', 'evidence')
        self.rows = [self.row(1, 'package', '2026-10-15T22:35:11Z'),
                     self.row(2, 'evidence', '2026-10-15T22:35:09Z'),
                     self.row(3, 'evidence', '2026-10-15T22:10:47Z')]
        self.listing = dict(total_count=3, artifacts=self.rows)

    def row(self, ident, name, created):
        return dict(id=ident, name=name, created_at=created, expired=False,
                    digest='sha256:' + 'b' * 64, size_in_bytes=100,
                    workflow_run=dict(id=123, head_sha='a' * 40))

    def archive(self, members):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            for name, value in members:
                archive.writestr(name, value)
        raw = output.getvalue()
        for name, _ in members:
            if isinstance(name, str) and '\\' in name:
                # Windows ZipInfo normalizes this while authoring the fixture.
                # Restore both local and central names without changing length.
                original = name.encode('utf-8')
                normalized = name.replace('\\', '/').encode('utf-8')
                if normalized in raw:
                    self.assertEqual(raw.count(normalized), 2)
                    raw = raw.replace(normalized, original)
                self.assertEqual(raw.count(original), 2)
        return raw

    def record(self, raw):
        return dict(id=73, size_in_bytes=len(raw), digest='sha256:' + hashlib.sha256(raw).hexdigest())

    def download(self, raw, destination, record=None):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            kwargs['stdout'].write(raw)
            return subprocess.CompletedProcess(args, 0, stderr=b'')
        with patch.object(self.mod.subprocess, 'run', side_effect=run):
            self.mod.download_artifact('example/research', record or self.record(raw), destination)
        return calls

    def test_successful_attempt_selects_new_evidence_and_preserves_first_failure(self):
        original = copy.deepcopy(self.listing)
        selected = self.mod.select_artifacts(self.selected, self.listing, self.names)
        self.assertEqual({name: value['id'] for name, value in selected.items()}, {'package': 1, 'evidence': 2})
        self.assertEqual(self.listing, original)
        self.assertEqual(selected['evidence']['digest'], self.rows[1]['digest'])

    def test_missing_ambiguous_expired_foreign_or_oversized_current_artifact_is_rejected(self):
        for defect in ('missing', 'ambiguous', 'expired', 'foreign_run', 'foreign_source', 'size', 'digest', 'after_end'):
            with self.subTest(defect=defect):
                listing = copy.deepcopy(self.listing)
                row = listing['artifacts'][1]
                if defect == 'missing': listing['artifacts'].pop(1); listing['total_count'] -= 1
                elif defect == 'ambiguous': listing['artifacts'].append(copy.deepcopy(row)); listing['total_count'] += 1
                elif defect == 'expired': row['expired'] = True
                elif defect == 'foreign_run': row['workflow_run']['id'] += 1
                elif defect == 'foreign_source': row['workflow_run']['head_sha'] = 'c' * 40
                elif defect == 'size': row['size_in_bytes'] = self.mod.MAX_ARCHIVE_BYTES + 1
                elif defect == 'digest': row['digest'] = None
                else: row['created_at'] = '2026-10-15T22:37:00Z'
                with self.assertRaises(ValueError):
                    self.mod.select_artifacts(self.selected, listing, self.names)

    def test_incomplete_listing_or_unqualified_attempt_cannot_select(self):
        for defect in ('listing', 'failure', 'pending', 'reversed', 'invalid_time', 'duplicate_id'):
            with self.subTest(defect=defect):
                selected, listing = copy.deepcopy(self.selected), copy.deepcopy(self.listing)
                if defect == 'listing': listing['total_count'] += 1
                elif defect == 'failure': selected['conclusion'] = 'failure'
                elif defect == 'pending': selected['status'] = 'in_progress'
                elif defect == 'reversed': selected['updated_at'] = '2026-10-15T22:00:00Z'
                elif defect == 'invalid_time': selected['run_started_at'] = 'unknown'
                else: listing['artifacts'][1]['id'] = 1
                with self.assertRaises(ValueError):
                    self.mod.select_artifacts(selected, listing, self.names)

    def test_download_uses_exact_id_and_validates_outer_bytes_before_extracting(self):
        raw = self.archive([('proof/result.json', b'artificial proof')])
        target = self.root / 'verified'
        calls = self.download(raw, target)
        self.assertEqual(calls, [['gh', 'api', 'repos/example/research/actions/artifacts/73/zip']])
        self.assertEqual((target / 'proof/result.json').read_bytes(), b'artificial proof')
        self.assertEqual(target.with_name('verified.artifact.zip').read_bytes(), raw)

    def test_changed_digest_or_size_keeps_bytes_without_extracting(self):
        raw = self.archive([('proof.json', b'artificial')])
        for defect in ('size', 'digest'):
            with self.subTest(defect=defect):
                record = self.record(raw)
                if defect == 'size': record['size_in_bytes'] += 1
                else: record['digest'] = 'sha256:' + '0' * 64
                target = self.root / defect
                with self.assertRaises(ValueError): self.download(raw, target, record)
                self.assertFalse(target.exists())
                self.assertEqual(target.with_name(target.name + '.artifact.zip').read_bytes(), raw)

    def test_unsafe_names_symlinks_and_case_collisions_never_extract(self):
        link = zipfile.ZipInfo('link')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [[(name, b'unsafe')] for name in ('../escape', '/absolute', 'C:/drive', 'a\\b', 'CON', 'a.')]
        cases += [[(link, b'../escape')], [('Case', b'a'), ('case', b'b')]]
        for index, members in enumerate(cases):
            with self.subTest(index=index):
                target = self.root / str(index)
                with self.assertRaises(ValueError): self.download(self.archive(members), target)
                self.assertFalse(target.exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_existing_preparation_is_not_overwritten_or_downloaded(self):
        target = self.root / 'existing'; target.mkdir()
        (target / 'keep').write_bytes(b'preserve')
        with patch.object(self.mod.subprocess, 'run') as call, self.assertRaises(ValueError):
            self.mod.download_artifact('example/research', self.record(b'x'), target)
        call.assert_not_called()
        self.assertEqual((target / 'keep').read_bytes(), b'preserve')

    def test_expanded_size_limit_applies_before_destination_creation(self):
        raw = self.archive([('proof.json', b'long artificial evidence')])
        target = self.root / 'bounded'
        with patch.object(self.mod, 'MAX_EXTRACTED_BYTES', 2), self.assertRaises(ValueError):
            self.download(raw, target)
        self.assertFalse(target.exists())

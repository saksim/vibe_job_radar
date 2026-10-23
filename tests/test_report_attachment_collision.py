"""Attachment names cannot alias core report files on case-insensitive volumes."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from test_core import NOW, job
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class ReportAttachmentCollisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'new-report'
        self.previous = self.root / 'previous-report.txt'
        self.previous.write_bytes(b'previous completed report')
        self.batch = Store(':memory:')
        self.addCleanup(self.batch.__exit__)
        self.batch.add(job())

    def reject(self, attachments):
        with self.assertRaisesRegex(ValueError, 'attachment conflicts'):
            analyze(self.batch, self.output, as_of=NOW,
                    json_attachments=lambda manifest: attachments)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.new-report-*')), [])
        self.assertEqual(self.previous.read_bytes(), b'previous completed report')
        self.assertEqual(len(self.batch.records()), 1)

    def test_attachment_cannot_alias_original_output_by_case(self):
        self.reject({'Candidate.Effective.json': {'unexpected': True}})

    def test_attachment_cannot_alias_reserved_manifest_by_case(self):
        self.reject({'RUN_MANIFEST.json': {'unexpected': True}})

    def test_distinct_mixed_case_attachment_keeps_all_original_files_and_hashes(self):
        attached = {'Audit.Note.json': {'source': 'artificial fixture'}, 'extra.json': {'retained': True}}
        manifest = analyze(self.batch, self.output, as_of=NOW,
                           json_attachments=lambda value: attached)
        hashes = manifest['output_files_sha256']
        self.assertEqual(len(hashes), 31)  # 29 original outputs and both attachments.
        self.assertEqual({path.name for path in self.output.iterdir()}, {*hashes, 'run_manifest.json'})
        for name, digest in hashes.items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        for name, content in attached.items():
            self.assertEqual(json.loads((self.output / name).read_text(encoding='utf-8')), content)
        self.assertEqual(json.loads((self.output / 'candidate.effective.json').read_text(encoding='utf-8')),
                         {'name': '待填写', 'evidence': []})
        self.assertEqual(json.loads((self.output / 'run_manifest.json').read_text(encoding='utf-8')), manifest)
        self.assertEqual(self.previous.read_bytes(), b'previous completed report')


if __name__ == '__main__':
    unittest.main()

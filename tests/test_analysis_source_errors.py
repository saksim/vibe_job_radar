"""Public analysis failures must be actionable without publishing partial reports."""
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import test_workbench as fixtures
from test_core import job
from vibe_job_radar.cli import main
from vibe_job_radar.store import SourceReadBusy, Store


class AnalysisHTTPErrorTests(unittest.TestCase):
    setUp = fixtures.HTTPTests.setUp
    tearDown = fixtures.HTTPTests.tearDown
    call = fixtures.HTTPTests.call

    def prepare(self):
        self.assertEqual(self.call('/api/job', fixtures.capture())[0], 200)
        return self.server.workspace.db.read_bytes()

    def assert_no_report(self):
        reports = self.server.workspace.root / 'reports'
        self.assertEqual(list(reports.iterdir()), [])

    def test_busy_analysis_returns_retry_message_and_preserves_source(self):
        before = self.prepare()
        with patch.object(Store, '_connect_read_snapshot', side_effect=SourceReadBusy('source database changed during read; retry when writes settle')):
            code, _, body = self.call('/api/analyze', {})
        self.assertEqual(code, 400, body)
        self.assertIn('稍后重试', json.loads(body)['error'])
        self.assertEqual(self.server.workspace.db.read_bytes(), before)
        self.assert_no_report()

    def test_invalid_database_returns_input_error_without_reconstruction(self):
        self.prepare()
        self.server.workspace.db.write_bytes(b'artificial damaged database')
        before = self.server.workspace.db.read_bytes()
        code, _, body = self.call('/api/analyze', {})
        self.assertEqual(code, 400, body)
        self.assertIn('数据库', json.loads(body)['error'])
        self.assertEqual(self.server.workspace.db.read_bytes(), before)
        self.assert_no_report()


    def test_source_copy_permission_error_is_actionable_and_preserves_source(self):
        before = self.prepare()
        with patch('vibe_job_radar.store.shutil.copyfile', side_effect=PermissionError('artificial source denial')):
            code, _, body = self.call('/api/analyze', {})
        self.assertEqual(code, 400, body)
        self.assertIn('文件权限', json.loads(body)['error'])
        self.assertEqual(self.server.workspace.db.read_bytes(), before)
        self.assert_no_report()

    def test_report_write_io_error_is_actionable_and_cleans_partial_output(self):
        before = self.prepare()
        with patch('vibe_job_radar.pipeline.atomic_text', side_effect=OSError('artificial report I/O')):
            code, _, body = self.call('/api/analyze', {})
        self.assertEqual(code, 400, body)
        self.assertIn('工作区', json.loads(body)['error'])
        self.assertEqual(self.server.workspace.db.read_bytes(), before)
        self.assert_no_report()


    def test_corrupt_demo_database_is_actionable_and_preserves_both_datasets(self):
        real_before = self.prepare()
        demo = self.server.workspace.demo_db
        demo.write_bytes(b'artificial damaged demo database')
        demo_before = demo.read_bytes()
        code, _, body = self.call('/api/analyze', {'dataset': 'demo'})
        self.assertEqual(code, 400, body)
        self.assertIn('数据库', json.loads(body)['error'])
        self.assertEqual(demo.read_bytes(), demo_before)
        self.assertEqual(self.server.workspace.db.read_bytes(), real_before)
        self.assert_no_report()

    def test_demo_initialization_permission_error_is_actionable(self):
        real_before = self.prepare()
        demo = self.server.workspace.demo_db
        with Store(demo):
            pass
        demo_before = demo.read_bytes()
        with patch('vibe_job_radar.workspace.Store', side_effect=PermissionError('artificial demo denial')) as initialize:
            code, _, body = self.call('/api/analyze', {'dataset': 'demo'})
        initialize.assert_called_once_with(demo)
        self.assertEqual(code, 400, body)
        self.assertIn('文件权限', json.loads(body)['error'])
        self.assertEqual(demo.read_bytes(), demo_before)
        self.assertEqual(self.server.workspace.db.read_bytes(), real_before)
        self.assert_no_report()


class AnalysisCLIErrorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'jobs.sqlite'
        self.output = self.root / 'report'
        with Store(self.path) as writer:
            writer.add(job())

    def command(self):
        return main(['analyze', '--db', str(self.path), '--out', str(self.output)])

    def test_busy_analysis_returns_error_code_without_traceback_or_output(self):
        before = self.path.read_bytes()
        stderr = StringIO()
        with redirect_stderr(stderr), patch.object(Store, '_connect_read_snapshot',
                side_effect=SourceReadBusy('source database changed during read; retry when writes settle')):
            code = self.command()
        self.assertEqual(code, 2)
        self.assertIn('retry when writes settle', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_database_returns_error_code_without_traceback_or_changes(self):
        self.path.write_bytes(b'artificial damaged database')
        before = self.path.read_bytes()
        stderr = StringIO()
        with redirect_stderr(stderr):
            code = self.command()
        self.assertEqual(code, 2)
        self.assertIn('DatabaseError', stderr.getvalue())
        self.assertNotIn('Traceback', stderr.getvalue())
        self.assertFalse(self.output.exists())
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()


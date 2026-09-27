"""Actual staged files and durable writes; synthetic data, no network."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.models import JobRecord
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class DurableReportWritesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db, self.output = self.root / 'fixture.sqlite', self.root / 'report'
        self.fixed = '2026-09-25T10:00:00+00:00'
        self.body = '职责：研发时间序列预测模型。要求：使用 Cursor 编写程序、单元测试并完成代码审查。'
        with Store(self.db) as store:
            store.add(JobRecord(title='时间序列算法工程师', text=self.body, source_mode='synthetic',
                                is_synthetic=True, collected_at=self.fixed))

    def analyze(self):
        return analyze(self.db, self.output, demo_mode=True, as_of=self.fixed)

    def start(self):
        results, errors = [], []
        def run():
            try:
                results.append(self.analyze())
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, results, errors

    def assert_clean(self):
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob('.report-*')), [])

    def test_four_writers_flush_every_export_before_manifest_and_publication(self):
        original = os.fsync
        lock, entered, release = threading.Lock(), threading.Event(), threading.Event()
        active = peak = completed = 0
        at_manifest = []
        def held(descriptor):
            nonlocal active, peak, completed
            if not threading.current_thread().name.startswith('radar-report-'):
                at_manifest.append((active, completed))
                return original(descriptor)
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    entered.set()
            try:
                if not release.wait(30):
                    raise AssertionError('fixture release missing')
                result = original(descriptor)
                with lock:
                    completed += 1
                return result
            finally:
                with lock:
                    active -= 1
        with patch.object(os, 'fsync', held):
            thread, results, errors = self.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertFalse(self.output.exists())
                staging = list(self.root.glob('.report-*'))
                self.assertEqual(len(staging), 1)
                self.assertFalse((staging[0] / 'run_manifest.json').exists())
                self.assertEqual(active, 4)
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peak, 4)
        hashes = results[0]['output_files_sha256']
        self.assertEqual(completed, len(hashes))
        self.assertEqual(at_manifest, [(0, len(hashes))])
        for name, digest in hashes.items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
        self.assertIn(self.body, (self.output / 'jobs.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(list(self.root.glob('.report-*')), [])

    def test_export_failure_waits_for_running_writers_before_cleanup(self):
        original = os.fsync
        lock, four, failed, release = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
        started = 0
        disk_error = OSError('independent synthetic disk failure')
        def held(descriptor):
            nonlocal started
            if not threading.current_thread().name.startswith('radar-report-'):
                return original(descriptor)
            with lock:
                number = started
                started += 1
                if started == 4:
                    four.set()
            if number == 0:
                if not four.wait(10):
                    raise AssertionError('other writers never started')
                failed.set()
                raise disk_error
            if not release.wait(30):
                raise AssertionError('fixture release missing')
            return original(descriptor)
        with patch.object(os, 'fsync', held):
            thread, results, errors = self.start()
            try:
                self.assertTrue(failed.wait(10))
                self.assertTrue(thread.is_alive())
                self.assertFalse(self.output.exists())
                self.assertEqual(len(list(self.root.glob('.report-*'))), 1)
            finally:
                release.set()
                thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0], disk_error)
        self.assert_clean()
        self.assertFalse(any(t.name.startswith(f'radar-report-{thread.ident}_') for t in threading.enumerate()))

    def test_manifest_failure_preserves_previous_report_and_leaves_no_partial_report(self):
        from vibe_job_radar import pipeline
        original = pipeline.atomic_json
        previous = self.root / 'previous-report.txt'
        previous.write_text('original report', encoding='utf-8')
        def fail_manifest(path, value):
            if path.name == 'run_manifest.json':
                raise OSError('synthetic manifest write failure')
            return original(path, value)
        with patch.object(pipeline, 'atomic_json', side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.analyze()
        self.assert_clean()
        self.assertEqual(previous.read_text(encoding='utf-8'), 'original report')

    def test_directory_commit_failure_does_not_leave_staging_or_partial_public_output(self):
        original = os.replace
        def fail_publish(source, target):
            if Path(target) == self.output:
                self.assertTrue((Path(source) / 'run_manifest.json').is_file())
                raise OSError('synthetic directory publication failure')
            return original(source, target)
        with patch.object(os, 'replace', side_effect=fail_publish):
            with self.assertRaises(OSError):
                self.analyze()
        self.assert_clean()


if __name__ == '__main__':
    unittest.main()

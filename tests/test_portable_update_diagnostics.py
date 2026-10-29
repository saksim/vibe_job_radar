"""Metadata and failed-handoff observations; never launches a built program."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import automatic_update_acceptance as acceptance
from build_windows_portable import write_portable_metadata
from portable_failure_diagnostics import AutomaticUpdateObservation
from vibe_job_radar.release_install import read_json
from vibe_job_radar.release_updates import UpdateError


class PortableUpdateDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_fixed_observation_freezes_before_cleanup_without_private_values(self):
        failure = {}
        observation = AutomaticUpdateObservation(failure)
        observation.phase = 'wait_ready'
        observation.helper = Mock(poll=Mock(return_value=5))
        observation.journal = self.root
        (self.root/'status.json').write_text(json.dumps(dict(status='failed', nonce='PRIVATE',
            backup='PRIVATE_PATH', error_type='PRIVATE_ERROR', rollback=False)), encoding='utf-8')
        (self.root/'ready.json').write_text(json.dumps(dict(status='ready', nonce='PRIVATE',
            port=45123, pid=123)), encoding='utf-8')
        observation.capture()
        before = dict(failure)
        observation.phase = 'cleanup'; observation.cleanup_started = True
        observation.helper.poll.return_value = 0
        observation.capture()
        self.assertEqual(failure, before)
        self.assertEqual(failure['journal_status'], 'failed')
        self.assertEqual(failure['ready_status'], 'ready')
        self.assertEqual(failure['helper_returncode'], 5)
        self.assertTrue(failure['captured_before_cleanup'])
        text = json.dumps(failure)
        self.assertNotIn('PRIVATE', text); self.assertNotIn(str(self.root), text)
        self.assertNotIn('nonce', text); self.assertNotIn('45123', text)

    def test_invalid_or_oversized_observations_are_bounded_and_never_reflect_input(self):
        failure = {}
        observation = AutomaticUpdateObservation(failure)
        observation.phase = 'PRIVATE_PHASE'
        observation.helper = Mock(poll=Mock(side_effect=RuntimeError('PRIVATE_ERROR')))
        observation.journal = self.root
        (self.root/'status.json').write_bytes(b' ' * 100001)
        (self.root/'ready.json').write_text('{"status":"PRIVATE_STATUS"}', encoding='utf-8')
        observation.capture()
        self.assertTrue(failure['observer_failed'])
        self.assertEqual(failure['phase'], 'unavailable')
        self.assertEqual(failure['journal_status'], 'unavailable')
        self.assertEqual(failure['ready_status'], 'unavailable')
        self.assertIsNone(failure['helper_returncode'])
        self.assertNotIn('PRIVATE', json.dumps(failure))

    def bundle(self, metadata):
        bundle = self.root/'bundle'; bundle.mkdir()
        (bundle/'PORTABLE.json').write_text(metadata, encoding='utf-8')
        (bundle/'VibeJobRadar.exe').write_bytes(b'artificial fixture; never executable')
        return bundle

    def test_setup_failure_reports_original_phase_without_starting_a_process(self):
        bundle = self.bundle(' ' * 100001)
        failure = {}
        with patch('verify_windows_portable.RunningApp') as app:
            with self.assertRaises(UpdateError):
                acceptance.verify(bundle, self.root/'acceptance', self.root, {}, failure=failure)
        app.assert_not_called()
        self.assertEqual(failure['phase'], 'read_manifest')
        self.assertFalse(failure['manifest_within_limit'])
        self.assertEqual(failure['manifest_size_bytes'], 100001)
        self.assertEqual(failure['manifest_limit_bytes'], 100000)
        self.assertFalse(failure['helper_started'])
        self.assertEqual(failure['journal_status'], 'absent')
        self.assertTrue(failure['captured_before_cleanup'])

    def test_helper_failure_captures_before_cleanup_preserving_exception_and_wait(self):
        bundle = self.bundle(json.dumps(dict(version='0.19.0', runtime_source={'revision':'a'*40},
            source={'sha256':'b'*64})))
        old = Mock(); old.proc.pid = 123
        helper = Mock(poll=Mock(return_value=4))
        process = Mock(); process.created = 42
        failure = {}; journal = []
        error = UpdateError('PRIVATE_ERROR')
        def fail_wait(path, *args, **kwargs):
            journal.append(path.parent)
            path.write_text('{"status":"armed","nonce":"PRIVATE"}', encoding='utf-8')
            raise error
        def cleanup():
            (journal[0]/'status.json').write_text('{"status":"failed"}', encoding='utf-8')
        old.close.side_effect = cleanup
        with patch('verify_windows_portable.RunningApp', return_value=old), \
             patch('vibe_job_radar.release_updates.stage_package', return_value=self.root/'staged'), \
             patch('vibe_job_radar.release_install.WindowsProcess', return_value=process), \
             patch('vibe_job_radar.release_install.wait_record', side_effect=fail_wait) as wait, \
             patch.object(acceptance.subprocess, 'Popen', return_value=helper), \
             patch.object(acceptance.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            with self.assertRaises(UpdateError) as caught:
                acceptance.verify(bundle, self.root/'acceptance', self.root, {}, failure=failure)
        self.assertIs(caught.exception, error)
        self.assertEqual(failure['phase'], 'wait_armed')
        self.assertEqual(failure['journal_status'], 'armed')
        self.assertEqual(failure['helper_returncode'], 4)
        self.assertTrue(failure['captured_before_cleanup'])
        old.close.assert_called_once(); helper.terminate.assert_not_called()
        wait.assert_called_once()
        self.assertEqual(wait.call_args.kwargs['timeout'], 20)
        self.assertNotIn('PRIVATE', json.dumps(failure))

    def test_compact_metadata_preserves_all_fields_at_legacy_byte_limit(self):
        # Source-shaped filenames/hash fields plus Unicode licenses; pretty JSON would
        # exceed the old reader's budget even though the complete compact form fits.
        value = dict(source={'sha256':'a'*64, 'files':{f'src/file_{n}.py':'b'*64 for n in range(1000)}},
            dependency_licenses=['licenses/许可证.txt'], runtime_source={'revision':'c'*40}, padding='')
        encode = lambda v: (json.dumps(v, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n').encode('utf-8')
        value['padding'] = 'x' * (100000-len(encode(value)))
        self.assertGreater(len(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')), 100000)
        path = self.root/'PORTABLE.json'
        write_portable_metadata(path, value)
        self.assertEqual(path.stat().st_size, 100000)
        self.assertEqual(read_json(path), value)
        self.assertEqual(path.read_bytes(), encode(value))

    def test_oversize_metadata_rejected_before_replacing_existing_file(self):
        path = self.root/'PORTABLE.json'; path.write_bytes(b'keep previous metadata')
        value = {'padding':'中'*40000}
        with self.assertRaises(ValueError), patch('build_windows_portable.atomic_text') as write:
            write_portable_metadata(path, value)
        write.assert_not_called()
        self.assertEqual(path.read_bytes(), b'keep previous metadata')
        with self.assertRaises(ValueError):
            write_portable_metadata(path, {'unsupported':float('nan')})
        self.assertEqual(path.read_bytes(), b'keep previous metadata')

    def test_growing_source_manifest_keeps_legacy_reader_and_all_evidence(self):
        source = {'algorithm':'sha256-file-manifest-v1', 'sha256':'a'*64, 'file_count':1000,
            'files':{f'src/vibe_job_radar/subsystem/file_{n}.py':'b'*64 for n in range(1000)}}
        value = {'source':source, 'runtime_source':{'raw_sha256':source['sha256']},
            'dependency_licenses':['licenses/许可证.txt'], 'upgrade_protocol':1}
        self.assertGreater(len(json.dumps(value, separators=(',', ':')).encode()), 100000)
        path = self.root/'PORTABLE.json'
        write_portable_metadata(path, value)
        meta = read_json(path)  # The existing installed reader's 100000-byte limit.
        self.assertEqual(meta['source_manifest'], 'SOURCE.json')
        self.assertEqual(meta['source'], {k:v for k,v in source.items() if k != 'files'})
        self.assertEqual(meta['runtime_source'], value['runtime_source'])
        self.assertEqual(meta['dependency_licenses'], value['dependency_licenses'])
        self.assertEqual(json.loads((self.root/'SOURCE.json').read_bytes()), source)
        self.assertIs(value['source'], source)

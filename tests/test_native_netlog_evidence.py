"""Connection cause evidence, private-data exclusion and observer transparency."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    import native_netlog_evidence as log
    from run_native_liepin_probe import SearchObserver
    from run_native_auth_probe import ObservedBackend

SECRET = 'PRIVATE_PASSWORD_URL_HEADER_BODY'
ENDPOINT = 'http://127.0.0.1:54321'
HOST = 'jobs.fixture.test'


def fixture():
    return {
        'constants': {'logEventTypes': {'URL_REQUEST_START_JOB': 1, 'TCP_CONNECT_ATTEMPT': 2,
                                      'SOCKET_POOL_BOUND_TO_SOCKET': 3, SECRET: 4},
                      'logEventPhase': {'PHASE_BEGIN': 1, 'PHASE_END': 2},
                      'logSourceType': {'URL_REQUEST': 1, 'SOCKET': 2},
                      'command_line': SECRET, 'headers': [SECRET]},
        'events': [
            {'type': 1, 'phase': 1, 'time': '1000',
             'source': {'id': 7, 'type': 1},
             'params': {'url': 'https://' + HOST + '/?password=' + SECRET,
                        'method': 'OPTIONS', 'headers': SECRET, 'body': SECRET}},
            {'type': 3, 'phase': 1, 'time': '1001',
             'source': {'id': 7, 'type': 1},
             'params': {'source_dependency': {'id': 8, 'type': 2}, 'secret': SECRET}},
            {'type': 2, 'phase': 1, 'time': '1002',
             'source': {'id': 8, 'type': 2},
             'params': {'address': '127.0.0.1:54321'}},
            {'type': 2, 'phase': 2, 'time': '1003',
             'source': {'id': 8, 'type': 2},
             'params': {'net_error': -102, 'os_error': 10061}},
            {'type': 1, 'phase': 2, 'time': '1004',
             'source': {'id': 7, 'type': 1},
             'params': {'net_error': -130}},
        ],
    }


class NativeNetLogEvidenceTests(unittest.TestCase):
    def test_proxy_failure_keeps_socket_cause_and_dependency_without_private_data(self):
        original = fixture()
        before = copy.deepcopy(original)
        result = log.reduce_netlog(original, ENDPOINT, {HOST})
        self.assertEqual(original, before)
        self.assertTrue(result['available'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['selected'], 5)
        rows = result['events']
        self.assertEqual(rows[0]['method'], 'OPTIONS')
        self.assertTrue(rows[0]['fixture_host'])
        self.assertEqual(rows[1]['dependency'], rows[2]['source'])
        self.assertTrue(rows[2]['owned_proxy_endpoint'])
        self.assertEqual((rows[3]['net_error'], rows[3]['os_error']), (-102, 10061))
        self.assertEqual(rows[4]['net_error'], -130)
        self.assertEqual(rows[4]['elapsed_ms'], 4)
        encoded = json.dumps(result)
        for value in (SECRET, ENDPOINT, HOST, '127.0.0.1', '54321', 'command_line', 'headers'):
            self.assertNotIn(value, encoded)

    def test_unknown_and_invalid_fields_cannot_escape_or_claim_known_cause(self):
        data = fixture()
        row = data['events'][0]
        row.update(type=4, phase=99, time='NaN', source={'id': SECRET, 'type': 99})
        row['params'].update(net_error=-9999, os_error=SECRET, method=[SECRET],
                             url='https://' + SECRET + '/', source_dependency={'id': True})
        data['events'].append(SECRET)
        result = log.reduce_netlog(data, ENDPOINT, {HOST})
        first = result['events'][0]
        self.assertEqual(first['event'], 'unknown')
        self.assertEqual(first['net_error'], -9999)
        self.assertEqual(first['phase'], 'unknown')
        for key in ('source', 'os_error', 'method', 'elapsed_ms', 'dependency'):
            self.assertIsNone(first[key])
        self.assertFalse(first['fixture_host'])
        self.assertEqual(result['malformed'], 1)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_bounded_tail_reports_discarded_events_without_success_claim(self):
        data = fixture()
        result = log.reduce_netlog(data, ENDPOINT, {HOST}, limit=2)
        self.assertEqual(result['selected'], 5)
        self.assertEqual(result['dropped'], 3)
        self.assertEqual(len(result['events']), 2)
        self.assertEqual(result['events'][-1]['net_error'], -130)
        self.assertFalse(result['complete'])
        for limit in (0, True, log.MAX_EVENTS + 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                log.reduce_netlog(data, ENDPOINT, {HOST}, limit=limit)

    def test_missing_invalid_and_oversize_files_are_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'private.json'
            self.assertFalse(log.read_netlog(path, ENDPOINT, {HOST})['available'])
            for raw in (b'{"events":', b'[]', b'{"constants": {}, "events": []}'):
                path.write_bytes(raw)
                self.assertFalse(log.read_netlog(path, ENDPOINT, {HOST})['available'])
            path.write_bytes(b'x' * 33)
            with patch.object(log, 'MAX_BYTES', 32), patch.object(log.json, 'loads') as loads:
                self.assertEqual(log.read_netlog(path, ENDPOINT, {HOST})['reason'], 'size_limit')
                loads.assert_not_called()

    def test_owned_files_cleaned_once_and_other_files_and_options_preserved(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(log, 'require_ci') as gate:
            root = Path(folder)
            neighbour = root / 'keep.txt'
            neighbour.write_text(SECRET)
            one = log.NativeNetLog(root, ENDPOINT, {HOST})
            two = log.NativeNetLog(root, ENDPOINT, {HOST})
            self.assertNotEqual(one.directory, two.directory)
            options = {'args': ['--existing'], 'headless': True, 'proxy': {'server': ENDPOINT}}
            before = copy.deepcopy(options)
            configured = one.options(options)
            self.assertEqual(options, before)
            self.assertEqual(configured['proxy'], options['proxy'])
            self.assertEqual(configured['args'][-2:], ['--net-log-capture-mode=Default', '--net-log-max-size-mb=16'])
            with self.assertRaises(ValueError):
                one.options({'args': ['--log-net-log=existing']})
            one.path.write_text(json.dumps(fixture()), encoding='utf8')
            first = one.finish()
            self.assertTrue(first['available'])
            self.assertEqual(first['private_cleanup'], 'removed')
            self.assertIs(one.finish(), first)
            self.assertFalse(one.directory.exists())
            self.assertTrue(two.directory.exists())
            self.assertEqual(neighbour.read_text(), SECRET)
            self.assertFalse(two.finish()['available'])
            self.assertEqual(gate.call_count, 2)

    def test_ci_gate_precedes_creating_any_private_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'not-created'
            with patch.object(log, 'require_ci', side_effect=SystemExit('CI required')):
                with self.assertRaises(SystemExit):
                    log.NativeNetLog(target, ENDPOINT, {HOST})
            self.assertFalse(target.exists())

    def test_observer_options_preserve_original_transport_when_setup_fails(self):
        backend = SearchObserver.__new__(SearchObserver)
        backend.probe = {}
        backend.tunnel = SimpleNamespace(endpoint=ENDPOINT)
        backend.contract = SimpleNamespace(hosts={HOST})
        options = {'args': ['--existing'], 'proxy': {'server': ENDPOINT}}
        with patch.object(ObservedBackend, '_launch_options', return_value=options) as launch, \
                patch('run_native_liepin_probe.NativeNetLog', side_effect=OSError(SECRET)):
            self.assertIs(backend._launch_options({}), options)
        launch.assert_called_once_with({})
        self.assertEqual(backend.probe['client_connection_log']['reason'], 'setup_failed')
        self.assertNotIn(SECRET, json.dumps(backend.probe))

    def test_close_error_is_preserved_even_when_reducing_fails(self):
        for broken in (False, True):
            with self.subTest(broken=broken):
                backend = SearchObserver.__new__(SearchObserver)
                backend.probe = {}
                backend._connection_log = Mock()
                if broken:
                    backend._connection_log.finish.side_effect = OSError(SECRET)
                else:
                    backend._connection_log.finish.return_value = log.unavailable('missing_or_invalid')
                original = RuntimeError('original close failure')
                with patch.object(ObservedBackend, 'close', side_effect=original):
                    with self.assertRaises(RuntimeError) as caught:
                        backend.close()
                self.assertIs(caught.exception, original)
                backend._connection_log.finish.assert_called_once_with()
                self.assertFalse(backend.probe['client_connection_log']['available'])
                self.assertNotIn(SECRET, json.dumps(backend.probe))


if __name__ == '__main__':
    unittest.main()

"""Exercise the actual acceptance failure path with no CA, browser or sockets."""
from contextlib import nullcontext, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    SPEC = importlib.util.spec_from_file_location('native_proxy_probe_under_test',
        ROOT / 'scripts/run_native_proxy_auth.py')
    probe = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(probe)
    import native_proxy_evidence as evidence

SECRET = 'PRIVATE_PROXY_VALUE_DO_NOT_WRITE'


class NativeProxyEvidenceTests(unittest.TestCase):
    def test_actual_probe_failure_records_source_count_and_pre_close_facts(self):
        state = SimpleNamespace(source=None, fixture=None, backends=[])
        class Source:
            def __init__(self, *args):
                self.server = SimpleNamespace(server_address=('127.0.0.1', 1))
                self.requests, self.sni = [], []
                self.closed = False
                state.source = self
            def close(self):
                self.closed = True

        class HTTP:
            protocol = 'http'
            def setUp(self):
                self.connects, self.authentication, self.greetings = [], [], []
                self.expected_proxy_auth = ''
            def perform(self, action):
                state.fixture = self
                return action()
            def tearDown(self): pass
            def doCleanups(self): pass

        class SOCKS(HTTP):
            protocol = 'socks5'

        class Backend:
            def __init__(self, *args, **kwargs):
                self.closed = False
                self.fixture = state.fixture
                self.browser = SimpleNamespace(version='ARTIFICIAL-VERSION')
                self.error = None
                self.tunnel = SimpleNamespace(_closed=False, connections=0, last_error='',
                    _sockets=set(), thread=SimpleNamespace(is_alive=lambda: not self.closed),
                    username=SECRET, password=SECRET, _authorization=SECRET)
                state.backends.append(self)
            def open(self, url):
                if self.fixture.protocol == 'socks5':
                    self.error = 'local_proxy_connection_failed'
                    try:
                        raise RuntimeError('Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://' + SECRET)
                    except RuntimeError as cause:
                        raise probe.CrawlError(self.error) from cause
                self.fixture.connects.append(SECRET)
                if self.fixture.expected_proxy_auth:
                    raise probe.CrawlError('local_proxy_auth_failed')
                self.tunnel.connections += 1
                count = 1 if url.endswith('/job/1') else 2
                for _ in range(count):
                    state.source.requests.append({'proxy_secret_absent': True,
                        'origin_secret_absent': True, 'private_body': SECRET})
                    state.source.sni.append(probe.HOST)
                return object()
            def close(self):
                self.closed = True
                self.tunnel._closed = True

        adapter = SimpleNamespace(search_url=lambda value: probe.URL + '/search',
            cards=lambda page: [{}], detail=lambda page: {'text': 'Cursor'})
        with tempfile.TemporaryDirectory() as temp:
            with patch.multiple(probe, ROOT=Path(temp), trust_fixture=lambda root: nullcontext(),
                    Fixture=Source, HTTPProxyAuthenticationTests=HTTP,
                    SocksProxyAuthenticationTests=SOCKS, NativeBackend=Backend,
                    adapter=lambda: adapter, RateLedger=lambda *a, **kw: None,
                    use_policy=lambda value: nullcontext()), \
                 patch.object(probe.NetworkPolicy, 'capture', return_value=None), \
                 patch.dict(probe.os.environ, {'CI': 'true'}), \
                 patch.object(sys, 'argv', ['probe', '--controlled']), \
                 patch.object(socket, 'create_connection', side_effect=AssertionError('unexpected socket')), \
                 patch.object(subprocess, 'run', side_effect=AssertionError('unexpected subprocess')), \
                 patch.object(subprocess, 'Popen', side_effect=AssertionError('unexpected subprocess')), \
                 redirect_stdout(io.StringIO()):
                with self.assertRaises(probe.CrawlError) as caught:
                    probe.main()
            self.assertEqual(caught.exception.code, 'local_proxy_connection_failed')
            result = json.loads((Path(temp) / 'browser-acceptance/native-proxy-auth/results.json').read_text(encoding='utf-8'))
        self.assertFalse(result['success'])
        self.assertEqual(result['source_requests'], 3)
        self.assertEqual(result['stage'], 'failed')
        self.assertEqual(result['last_active_stage'], 'socks5-normal')
        failure = result['failure']
        self.assertEqual((failure['stage'], failure['action']), ('socks5-normal', 'search'))
        self.assertEqual(failure['browser_error_code'], 'local_proxy_connection_failed')
        self.assertTrue(failure['connection_facts']['guard_thread_alive'])
        self.assertFalse(failure['connection_facts']['guard_closed'])
        self.assertEqual(failure['connection_facts']['guard_connections'], 0)
        self.assertEqual(failure['connection_facts']['stage_source_requests'], 0)
        self.assertEqual([row['protocol'] for row in result['checks']], ['http'])
        self.assertEqual(result['checks'][0]['rejected_auth_attempts'], 1)
        self.assertEqual(result['checks'][0]['rejected_target_requests'], 0)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(state.source.closed)
        self.assertEqual(len(state.backends), 3)
        self.assertTrue(all(backend.closed for backend in state.backends))

    def test_snapshot_keeps_only_counts_and_whitelisted_values(self):
        hidden = type(SECRET, (Exception,), {})('Page.goto: ' + SECRET)
        hidden.code = SECRET
        guard = SimpleNamespace(connections=2, _sockets={SECRET}, _closed=False,
            last_error=SECRET, username=SECRET, password=SECRET, endpoint=SECRET,
            thread=SimpleNamespace(is_alive=lambda: True))
        backend = SimpleNamespace(tunnel=guard, error=SECRET)
        fixture = SimpleNamespace(greetings=[SECRET], authentication=[(SECRET, SECRET)],
            connects=[SECRET, SECRET])
        source = SimpleNamespace(requests=[{'body': SECRET, 'headers': SECRET}] * 3)
        result = {}
        evidence.update_proxy_evidence(result, 'socks5-normal', source=source, fixture=fixture,
            backend=backend, source_start=2, error=hidden, action='search')
        text = json.dumps(result)
        self.assertNotIn(SECRET, text)
        self.assertEqual(result['failure']['error_type'], 'other')
        self.assertEqual(result['failure']['error_code'], 'unknown')
        facts = result['failure']['connection_facts']
        self.assertEqual(facts['guard_error_code'], 'unknown')
        self.assertEqual(facts['backend_error_code'], 'unknown')
        self.assertEqual(facts['source_requests'], 3)
        self.assertEqual(facts['stage_source_requests'], 1)
        self.assertEqual(facts['upstream_authentications'], 1)
        self.assertEqual(facts['guard_active_sockets'], 1)
        self.assertEqual(len(result['connection_facts']), 1)
        self.assertEqual(len(facts), 12)

    def test_final_checkpoint_preserves_first_failure_and_reports_later_total(self):
        source = SimpleNamespace(requests=[1, 2])
        result = {}
        error = RuntimeError(SECRET)
        evidence.update_proxy_evidence(result, 'http-normal', source=source,
            error=error, action='create')
        first = json.loads(json.dumps(result['failure']))
        source.requests.append(3)
        evidence.update_proxy_evidence(result, 'failed', source=source,
            error=ValueError(SECRET), action='setup_or_cleanup')
        self.assertEqual(result['failure'], first)
        self.assertEqual(result['source_requests'], 3)
        self.assertEqual(result['last_active_stage'], 'http-normal')
        self.assertFalse(first['connection_facts']['guard_present'])
        self.assertNotIn(SECRET, json.dumps(result))
        with self.assertRaisesRegex(ValueError, '^unsupported native proxy evidence field$'):
            evidence.update_proxy_evidence(result, SECRET)

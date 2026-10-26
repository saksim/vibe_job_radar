"""Failure capture through the actual PAC script; no browser, CA or network."""
from contextlib import ExitStack, nullcontext
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT/'scripts'), str(ROOT/'tests'), *sys.path]):
    import run_native_pac_browser as pac
from vibe_job_radar.guided.contracts import PageSnapshot

SECRET = 'PRIVATE_ARTIFICIAL_OBSERVER_CONTENTS'


def capture_case(*, refusal_dials=False, fault=None):
    """Replace physical resources, retaining main/observed backend/shared observers."""
    events, backends, writes = [], [], []
    objects = {}

    def forbidden(*args, **kwargs):
        raise AssertionError('No socket or subprocess is allowed in this source fixture')

    class Source:
        def __init__(self):
            self.requests = []
            self.source = SimpleNamespace(config_id='authored-source')
        def close(self): events.append('source.close')

    class Pages:
        def __init__(self, *args):
            self.requests, self.sni = [], []
            self.server = SimpleNamespace(server_address=('127.0.0.1', 0))
            objects['pages'] = self
        def close(self): events.append('pages.close')

    class Proxy:
        def setUp(self):
            self.dials, self.connects = [], []
            self.setting, self.symbol = 'http://127.0.0.1:1', '93.184.216.34'
            self.proxy = SimpleNamespace(server_address=('127.0.0.1', 1))
            objects['proxy'] = self
        def perform(self, operation): return operation()
        def tearDown(self): events.append('proxy.tearDown')
        def doCleanups(self): events.append('proxy.doCleanups')

    class Workspace:
        def __init__(self, *args): pass
        def network_domain_pac_preferences(self, value): pass
        def network_domain_pac_dns_preferences(self, value): pass
        def network_policy(self): return pac.NetworkPolicy()
        def close_network(self): events.append('workspace.close_network')

    def init_backend(self, *args, **kwargs):
        self.kind = 'normal' if not backends else 'refusal'
        self.error = ''
        self._auth_attempts = set()
        self.browser = SimpleNamespace(version='authored; no-browser')
        self.tunnel = SimpleNamespace(last_error='', connections=0,
            server=SimpleNamespace(get_request=lambda: None, process_request=lambda *a: None),
            _handle=lambda *a: None, _reply=lambda *a: None, _open=lambda *a: None)
        backends.append(self)

    def open_backend(self, url):
        if self.kind == 'refusal':
            # Exercise the actual installed GuardEvidence/HandshakeTrace wiring.
            self.tunnel._reply(None, 407)
            if refusal_dials:
                objects['proxy'].dials.append(objects['proxy'].proxy.server_address)
            self.error = self.tunnel.last_error = 'local_proxy_connection_failed'
            raise pac.CrawlError(self.error)
        objects['pages'].requests.append('authored-read')
        objects['proxy'].dials.append(objects['proxy'].proxy.server_address)
        html = ('<h1>人工岗位</h1><div class="job-description">岗位职责：使用 Cursor 辅助开发和审查代码。'
                '任职要求：熟悉 Python 与数据库设计，负责时间序列预测系统。</div>'
                if url.endswith('/job/1') else '<a href="/job/1">人工岗位</a>')
        return PageSnapshot(url, html)

    def close_backend(self):
        events.append(self.kind + '.close')
        self.error = self.tunnel.last_error = 'closed_after_snapshot'
        self._guard_evidence = self._handshake_trace = None

    real_write = Path.write_text

    def write(path, data, *args, **kwargs):
        if path.name == 'results.json':
            stage = json.loads(data)['stage']
            if ((fault == 'checkpoint_write' and stage.endswith('-refusal-observed'))
                    or (fault == 'final_write' and stage == 'failed')):
                writes.append(stage)
                raise OSError(SECRET)
        return real_write(path, data, *args, **kwargs)

    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        stack.enter_context(patch.object(socket, 'socket', forbidden))
        stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        stack.enter_context(patch.object(pac, 'ROOT', root))
        stack.enter_context(patch.object(pac, 'SourceServer', Source))
        stack.enter_context(patch.object(pac, 'Fixture', Pages))
        stack.enter_context(patch.object(pac, 'RealProxyTests', Proxy))
        stack.enter_context(patch.object(pac, 'RealSocksTests', Proxy))
        stack.enter_context(patch.object(pac, 'Workspace', Workspace))
        stack.enter_context(patch.object(pac, 'RateLedger', lambda *a, **k: object()))
        stack.enter_context(patch.object(pac, 'trust_fixture', lambda *a: nullcontext()))
        stack.enter_context(patch.object(pac.NativeBackend, '__init__', init_backend))
        stack.enter_context(patch.object(pac.NativeBackend, 'open', open_backend))
        stack.enter_context(patch.object(pac.NativeBackend, 'close', close_backend))
        stack.enter_context(patch.object(Path, 'write_text', write))
        stack.enter_context(patch.object(sys, 'argv', ['run_native_pac_browser.py', '--controlled', '--portable-domain']))
        stack.enter_context(patch.dict(pac.os.environ, {'CI': 'true', 'GITHUB_ACTIONS': 'true'}))
        # The engine is not exercised by these physical-resource stubs. Choose
        # the cross-platform entry without changing global OS/path semantics.
        stack.enter_context(patch.object(pac.pac_url_worker, 'available', return_value=True))
        if fault == 'snapshot':
            stack.enter_context(patch.object(pac, 'guard_transport_facts', side_effect=RuntimeError(SECRET)))
        error = None
        try:
            pac.main()
        except Exception as exc:
            frames = traceback.extract_tb(exc.__traceback__)
            error = (type(exc), str(exc), frames[-1].line)
        saved = (root/'browser-acceptance/native-portable-domain-pac/results.json').read_text(encoding='utf-8')
        return error, json.loads(saved), saved, events, writes


class NativePacCaptureTests(unittest.TestCase):
    def check_cleanup(self, events):
        for event in ('normal.close', 'refusal.close', 'workspace.close_network',
                      'proxy.tearDown', 'proxy.doCleanups', 'pages.close', 'source.close'):
            self.assertEqual(events.count(event), 1, event)

    def test_before_close_observation_keeps_original_dial_failure(self):
        error, result, raw, events, writes = capture_case()
        self.assertEqual(error, (AssertionError, '', 'assert len(fixture.dials)>before'))
        self.assertFalse(result['success'])
        observation = result['refusal_observation']
        self.assertFalse(observation['capture_failed'])
        self.assertEqual(observation['controller_code'], 'local_proxy_connection_failed')
        self.assertTrue(observation['guard_transport']['available'])
        self.assertTrue(observation['guard_transport']['complete'])
        self.assertEqual(observation['guard_transport']['operations']['reply']['returned'], 1)
        self.assertTrue(observation['handshake_trace']['complete'])
        self.assertEqual([row['event'] for row in observation['handshake_trace']['events']],
                         ['guard.reply.407.started', 'guard.reply.407.returned'])
        self.assertNotIn(SECRET, raw)
        self.assertEqual(writes, [])
        self.check_cleanup(events)

    def test_capture_fault_never_overrides_failure_or_qualifies_missing_proof(self):
        cases = [(fault, dials) for fault in ('snapshot', 'checkpoint_write') for dials in (False, True)]
        cases.append(('final_write', False))
        for fault, dials in cases:
            with self.subTest(fault=fault, dials=dials):
                error, result, raw, events, writes = capture_case(refusal_dials=dials, fault=fault)
                self.assertIs(error[0], AssertionError)
                if dials:
                    self.assertEqual(error[1], 'required PAC refusal observation unavailable')
                else:
                    self.assertEqual(error[2], 'assert len(fixture.dials)>before')
                self.assertFalse(result['success'])
                if fault != 'final_write':
                    observation = result['refusal_observation']
                    self.assertTrue(observation['capture_failed'])
                    self.assertFalse(observation['guard_transport']['available'])
                    self.assertIsNone(observation['guard_transport']['operations'])
                    self.assertFalse(observation['handshake_trace']['available'])
                    self.assertIsNone(observation['handshake_trace']['events'])
                self.assertEqual(len(writes), 0 if fault == 'snapshot' else 1)
                self.assertNotIn(SECRET, raw)
                self.check_cleanup(events)

"""Policy ownership across callback contexts; mock only SDK startup and TLS I/O.

Real workspace consent, resolver/cache, quotas and browser route handler run here.
The separate browser acceptance script exercises the actual Playwright dispatcher.
"""
from __future__ import annotations

from contextvars import Context
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, MagicMock, patch

from test_encrypted_dns import HOST, IP4, IP6, fake_answers, wire
from vibe_job_radar.dns_wire import parse_answer, ResolutionError
from vibe_job_radar.guided.adapters import DOMAdapter
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.transport import PinnedTransport
from vibe_job_radar.network_policy import NetworkPolicy, use_policy, current_policy
from vibe_job_radar.network_settings import save as save_settings
from vibe_job_radar.workspace import Workspace


class BrowserPolicyBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = Workspace(self.root)
        save_settings(self.workspace, {'mode':'fake_ip_doh', 'revision':0, 'consent':True})
        self.adapter = DOMAdapter('fixture', 'Artificial fixture', (HOST,),
            f'https://{HOST}/search', 'q', r'^/job/[0-9]+$', f'https://{HOST}/login', (HOST,))
        self.ledger = RateLedger(self.root/'rates.sqlite', Limits(page_interval=0,request_interval=0))
        self.cancel = threading.Event()
        self.runtime = MagicMock()
        exe = self.root/'fixture-browser'; exe.touch()
        self.runtime.chromium.executable_path = str(exe)
        sync = Mock(); sync.return_value.start.return_value = self.runtime
        mod = types.ModuleType('playwright.sync_api'); mod.sync_playwright = sync
        self.patches = [
            patch.dict(sys.modules, {'playwright':types.ModuleType('playwright'), 'playwright.sync_api':mod}),
            patch('vibe_job_radar.guided.browser.environment_report', side_effect=lambda: {
                'playwright_version':'1.57.0', 'launch_tested':False, 'ready':False}),
            patch('urllib.request.getproxies', return_value={}),
            patch.dict(os.environ, {'VIBE_RADAR_HTTP_PROXY':'', 'VIBE_RADAR_SOCKS_PROXY':''}),
        ]
        for p in self.patches: p.start(); self.addCleanup(p.stop)
        self.policy = self.workspace.network_policy()
        self.backends = []
        self.addCleanup(lambda: [b.close() for b in self.backends])

    def backend(self, policy=None):
        with use_policy(policy or self.policy):
            backend = PlaywrightBackend(self.adapter, self.ledger, self.cancel)
        self.backends.append(backend)
        return backend

    @staticmethod
    def route(kind='document', method='GET'):
        r = Mock()
        r.request.url = f'https://{HOST}/search?q=private-query'
        r.request.resource_type = kind; r.request.method = method
        r.request.all_headers.return_value = {}
        r.request.post_data_buffer = None
        return r

    @staticmethod
    def exchange(host, kind, *args, **kwargs):
        return parse_answer(wire(host=host, kind=kind), host, kind)

    def invoke(self, backend, *, error=None, answers=None, kind='document'):
        route = self.route(kind)
        result = Mock(); result.status = 200
        result.getheaders.return_value = [('Content-Type','text/plain')]
        result.read.return_value = b'User-agent: *\nAllow: /\n'
        conn = Mock(); conn.getresponse.return_value = result
        with patch('socket.getaddrinfo', return_value=answers or fake_answers('198.18.4.136')), \
             patch.object(backend.wire.network_policy.resolver if backend.wire.network_policy else self.workspace.dns_resolver,
                          '_exchange', side_effect=error or self.exchange) as exchange, \
             patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection', return_value=conn) as connection:
            # A dispatcher coroutine/greenlet can start with no caller ContextVar.
            Context().run(backend._route, route)
        return route, exchange, connection

    def test_backend_binds_workspace_before_first_callback(self):
        backend = self.backend()
        self.assertIs(backend.wire.network_policy, self.policy)
        self.assertIs(backend.wire.network_policy.resolver, self.workspace.dns_resolver)

    def test_empty_callback_context_still_resolves_fake_ip_and_serves_page(self):
        backend = self.backend()
        self.assertFalse(Context().run(current_policy).encrypted_dns)
        route, exchange, connection = self.invoke(backend)
        self.assertIsNone(backend.error)
        route.abort.assert_not_called(); route.fulfill.assert_called_once()
        self.assertEqual(exchange.call_count, 2)  # One paired lookup, reused for page.
        self.assertEqual(connection.call_count, 2)  # robots and document, same policy.
        self.assertEqual(self.ledger.summary('fixture')['request']['day'], 2)
        self.assertIs(backend.wire.network_policy, self.policy)

    def test_xhr_and_resource_use_same_bound_resolver(self):
        for kind in ('xhr','fetch','script','stylesheet'):
            with self.subTest(kind=kind):
                backend = self.backend()
                route, _, connection = self.invoke(backend, kind=kind)
                route.fulfill.assert_called_once(); route.abort.assert_not_called()
                connection.assert_called_once()
                self.assertIs(backend.wire.network_policy, self.policy)

    def test_callback_does_not_rediscover_static_route(self):
        backend = self.backend()
        with patch('urllib.request.getproxies', side_effect=AssertionError('must not rediscover')):
            route, _, _ = self.invoke(backend)
        route.fulfill.assert_called_once()

    def test_robots_failure_is_still_enforced(self):
        backend = self.backend()
        route = self.route()
        with patch.object(backend.wire, 'ensure_robots', side_effect=CrawlError('robots_denied')), \
             patch.object(backend.wire,'fetch') as fetch:
            Context().run(backend._route, route)
        self.assertEqual(backend.error, 'robots_denied')
        fetch.assert_not_called(); route.abort.assert_called_once()

    def test_private_or_mixed_answers_remain_rejected(self):
        for values in [('10.0.0.1',), ('198.18.1.2','127.0.0.1')]:
            with self.subTest(values=values):
                backend = self.backend()
                route, exchange, conn = self.invoke(backend, answers=fake_answers(*values))
                self.assertEqual(backend.error, 'non_public_address')
                exchange.assert_not_called(); conn.assert_not_called(); route.fulfill.assert_not_called()

    def test_certificate_failure_not_converted_into_success(self):
        backend = self.backend()
        route, exchange, conn = self.invoke(backend, error=ResolutionError('encrypted_dns_tls_failed'))
        self.assertEqual(backend.error, 'encrypted_dns_tls_failed')
        exchange.assert_called_once(); conn.assert_not_called(); route.fulfill.assert_not_called()

    def test_revocation_stops_bound_resolver_even_with_cached_answers(self):
        backend = self.backend()
        route, _, _ = self.invoke(backend); route.fulfill.assert_called_once()
        save_settings(self.workspace, {'mode':'system', 'revision':1, 'consent':False})
        backend.error = None
        route, exchange, conn = self.invoke(backend, kind='fetch')
        self.assertEqual(backend.error, 'encrypted_dns_disabled')
        exchange.assert_not_called(); conn.assert_not_called(); route.fulfill.assert_not_called()

    def test_two_workspaces_do_not_share_consent_or_resolver(self):
        other = Workspace(self.root/'other'); other_policy = other.network_policy()
        backend = self.backend(); disabled = self.backend(other_policy)
        self.assertIsNot(backend.wire.network_policy.resolver, disabled.wire.network_policy.resolver)
        route, _, _ = self.invoke(backend); route.fulfill.assert_called_once()
        route, exchange, conn = self.invoke(disabled)
        self.assertEqual(disabled.error,'non_public_address')
        exchange.assert_not_called(); conn.assert_not_called(); route.fulfill.assert_not_called()

    def test_existing_session_not_upgraded_when_preferences_change(self):
        disabled_policy = NetworkPolicy(resolver=self.workspace.dns_resolver)
        disabled = self.backend(disabled_policy)
        route, _, conn = self.invoke(disabled)
        self.assertEqual(disabled.error,'non_public_address'); conn.assert_not_called()
        enabled = self.backend(self.workspace.network_policy())
        route, _, _ = self.invoke(enabled); route.fulfill.assert_called_once()

    def test_production_wire_rejects_rebinding_to_different_policy(self):
        backend = self.backend()
        with self.assertRaises(ValueError): backend.wire.bind_policy(NetworkPolicy())
        backend.wire.bind_policy(self.policy)  # identical object is idempotent
        self.assertIs(backend.wire.network_policy,self.policy)

    def test_bind_does_not_dial_or_accept_untyped_configuration(self):
        wire = PinnedTransport(self.adapter,self.ledger,self.cancel)
        with patch('socket.create_connection') as dial:
            with self.assertRaises(TypeError):wire.bind_policy({'encrypted_dns':True})
            wire.bind_policy(self.policy)
        dial.assert_not_called()

    def test_login_post_boundary_unchanged(self):
        backend = self.backend()
        route = self.route(method='POST')
        with patch.object(backend.wire,'fetch') as fetch:
            Context().run(backend._route,route)
        self.assertEqual(backend.error,'write_not_allowed');fetch.assert_not_called()




class PolicyBindingAcceptanceCleanupTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        script = Path(__file__).resolve().parents[1]/'scripts/run_browser_policy_binding.py'
        spec = importlib.util.spec_from_file_location('policy_binding_cleanup_test',script)
        self.script = importlib.util.module_from_spec(spec);spec.loader.exec_module(self.script)
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def idle(self, service):
        import time
        end = time.monotonic()+10
        while service.state()['busy'] and time.monotonic()<end:
            time.sleep(.01)
        self.assertFalse(service.state()['busy'])
        return service.state()['jobs'][0]

    def service(self):
        from test_guided import FakeBackend,fixture_adapter
        from test_guided_ownership import QUERY
        from vibe_job_radar.guided.adapters import Registry
        from vibe_job_radar.guided.service import GuidedService
        workspace = Workspace(self.root/'workspace')
        service = GuidedService(workspace,registry=Registry([fixture_adapter()]),
            backend_factory=FakeBackend)
        self.addCleanup(service.close)
        ident = service.create(QUERY)['id'];self.idle(service)
        return service,ident

    def test_explicit_stop_waits_for_browser_and_preserves_task_outputs(self):
        service,ident = self.service()
        backend = service._backends[ident];close = backend.close
        task = service._load(ident)
        service.action({'id':ident,'action':'collect','selected':[task['cards'][0]['id']]})
        self.idle(service);task = service._load(ident)
        self.assertEqual(task['status'],'completed')
        report = service.workspace.root/'reports'/task['report_id']
        before = {p.relative_to(report):p.read_bytes() for p in report.rglob('*') if p.is_file()}
        rates = service.ledger.summary('fixture');requests = list(backend.opens)
        entered,release = threading.Event(),threading.Event()
        values,errors = [],[]
        def held_close():
            entered.set()
            if not release.wait(20):raise AssertionError('fixture close was not released')
            close()
        def finish():
            try:values.append(self.script.stop_retained_task(service,ident,self.idle))
            except BaseException as exc:errors.append(exc)
        waiter = threading.Thread(target=finish)
        try:
            with patch.object(backend,'close',held_close):
                waiter.start();self.assertTrue(entered.wait(5))
                self.assertTrue(waiter.is_alive())
                self.assertIsNotNone(service._ownership.lease)
                release.set();waiter.join(10)
                self.assertFalse(waiter.is_alive());self.assertFalse(errors,errors)
            self.assertEqual(values[0]['status'],'stopped')
            saved = service._load(ident)
            self.assertEqual(saved['outcome'],task['outcome'])
            self.assertEqual(saved['report_id'],task['report_id'])
            self.assertEqual(before,{p.relative_to(report):p.read_bytes() for p in report.rglob('*') if p.is_file()})
            self.assertEqual(rates,service.ledger.summary('fixture'))
            self.assertEqual(requests,backend.opens)
            self.assertTrue(backend.closed)
            self.assertFalse(service._backends);self.assertIsNone(service._ownership.lease)
        finally:
            release.set();waiter.join(10)

    def test_close_failure_is_rejected_instead_of_counted_as_stopped(self):
        service,ident = self.service();backend = service._backends[ident]
        try:
            with patch.object(backend,'close',side_effect=RuntimeError('artificial close failure')):
                with self.assertRaises(AssertionError):
                    self.script.stop_retained_task(service,ident,self.idle)
            self.assertTrue(service.state()['closure_uncertain'])
            self.assertIsNotNone(service._ownership.lease)
        finally:
            # This artificial backend has no external process. Reclaim only
            # after the real worker exits, leaving production behavior intact.
            backend.close();service.close()
            self.assertIsNone(service._thread)
            service._ownership.release()

    def test_saved_result_requires_body_and_fixture_cleanup_to_finish(self):
        import json
        for failure in (None,'body','cleanup'):
            with self.subTest(failure=failure):
                out = self.root/(failure or 'success');out.mkdir()
                original = object()
                handler = types.SimpleNamespace(do_GET=object())
                result = {'success':True,'checks':['original business assertions']}
                def teardown():
                    self.assertIs(handler.do_GET,original)
                    self.assertFalse(result['success'])
                    self.assertFalse((out/'results.json').exists())
                    if failure=='cleanup':raise PermissionError('artificial upstream cleanup failure')
                fixture = types.SimpleNamespace(
                    origin=types.SimpleNamespace(RequestHandlerClass=handler),tearDown=teardown)
                def run():
                    with self.script.record_after_cleanup(result,fixture,original,out):
                        self.assertFalse(result['success'])
                        if failure=='body':raise RuntimeError('artificial workspace cleanup failure')
                if failure is None:run()
                else:
                    with self.assertRaises(RuntimeError if failure=='body' else PermissionError):run()
                saved = json.loads((out/'results.json').read_bytes())
                self.assertEqual(saved['success'],failure is None)
                self.assertEqual(saved['checks'],['original business assertions'])
                if failure=='body':self.assertEqual(saved['error_type'],'RuntimeError')
                if failure=='cleanup':self.assertEqual(saved['cleanup_error_type'],'PermissionError')


if __name__ == '__main__':unittest.main()

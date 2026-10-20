"""Controlled outages preserve honest stale data, never permission/TLS failures."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_local_public as local_fixture
import test_public_hybrid as public_fixture
import test_team_service as team_fixture
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.local_public import LocalPublicDataClient
from vibe_job_radar.network import FetchError
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.public_data import PublicDataClient
from vibe_job_radar.public_gateway import PublicGateway
from vibe_job_radar.public_tasks import PublicTasks
from vibe_job_radar.team_client import AuthenticatedPublicTransport
from vibe_job_radar.workspace import Workspace


class PublicOutageTests(unittest.TestCase):
    def client(self, local):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        now = [time.time()]; wire = Mock()
        if local:
            wire.json.return_value = local_fixture.payload()
            client = LocalPublicDataClient(Workspace(folder.name), transport=wire, clock=lambda: now[0])
            query = local_fixture.query()
        else:
            gateway = PublicGateway(public_fixture.REGISTRY, [public_fixture.job()], clock=lambda: now[0])
            wire.json.return_value = gateway.search(public_fixture.query().payload())
            client = PublicDataClient(Path(folder.name), 'https://service.fixture.test', public_fixture.REGISTRY,
                                      transport=wire, clock=lambda: now[0])
            query = public_fixture.query()
        return client, query, wire, now

    def test_transient_proxy_and_dns_outages_keep_original_stale_snapshot(self):
        for local in (True, False):
            client, query, wire, now = self.client(local)
            first = client.search(query, consent=True)
            for code in ('local_socks_connection_failed', 'local_socks_timeout', 'vm_proxy_connection_failed',
                         'vm_proxy_timeout', 'encrypted_dns_unavailable', 'encrypted_dns_timeout',
                         'encrypted_dns_cooldown', 'encrypted_dns_budget'):
                with self.subTest(local=local, code=code):
                    now[0] += 601; wire.json.side_effect = FetchError(code)
                    calls = wire.json.call_count
                    result = client.search(query, consent=True)
                    self.assertTrue(result['stale']); self.assertTrue(result['cache_reused'])
                    self.assertEqual(result['observed_at'], first['observed_at'])
                    self.assertEqual(result['response']['jobs'], first['response']['jobs'])
                    self.assertEqual(result['refresh_error'], code)
                    self.assertEqual(wire.json.call_count, calls + 1)
                    self.assertFalse(client.failure_guard.path.exists())

    def test_proxy_auth_protocol_and_unknown_failure_still_block_later_outage(self):
        for local in (True, False):
            for code in ('local_socks_auth_unsupported', 'local_socks_protocol_error', 'local_socks_request_rejected', 'unknown_proxy_error'):
                with self.subTest(local=local, code=code):
                    client, query, wire, now = self.client(local)
                    client.search(query, consent=True); now[0] += 601
                    wire.json.side_effect = FetchError(code)
                    with self.assertRaisesRegex(FetchError, code): client.search(query, consent=True)
                    now[0] += 601; wire.json.side_effect = FetchError('local_socks_timeout')
                    with self.assertRaisesRegex(FetchError, code): client.search(query, consent=True)
                    self.assertEqual(client.failure_guard.read(now[0]), code)

    def test_no_cache_or_expired_cache_does_not_claim_success(self):
        for local in (True, False):
            client, query, wire, now = self.client(local)
            wire.json.side_effect = FetchError('vm_proxy_timeout')
            with self.assertRaisesRegex(FetchError, 'vm_proxy_timeout'): client.search(query, consent=True)
            now[0] += 601; wire.json.side_effect = None
            client.search(query, consent=True); now[0] += 7 * 86400 + 1
            wire.json.side_effect = FetchError('local_socks_timeout')
            with self.assertRaisesRegex(FetchError, 'local_socks_timeout'): client.search(query, consent=True)


class TeamPublicOutageTests(unittest.TestCase):
    setUp = team_fixture.TeamTests.setUp
    connection = team_fixture.TeamTests.connection

    def public(self):
        connection = self.connection('alice', public=True)
        workspace = Workspace(self.root / 'alice')
        client = PublicDataClient(workspace.root / 'team_public_cache', connection.origin, connection.registry,
                                  transport=AuthenticatedPublicTransport(connection), clock=lambda: self.now[0])
        return connection, workspace, client

    def test_team_transient_transport_errors_reach_stale_cache_without_retry(self):
        connection, workspace, client = self.public()
        first = client.search(public_fixture.query(), consent=True)
        for code in ('http_503', 'network_error', 'local_socks_timeout', 'vm_proxy_connection_failed'):
            with self.subTest(code=code):
                self.now[0] += 601
                with patch.object(self.http, 'json', side_effect=FetchError(code, 'PRIVATE DETAIL')) as wire:
                    result = client.search(public_fixture.query(), consent=True)
                wire.assert_called_once()
                self.assertTrue(result['stale']); self.assertTrue(result['cache_reused'])
                self.assertEqual(result['observed_at'], first['observed_at'])
                self.assertEqual(result['response']['jobs'], first['response']['jobs'])
                self.assertEqual(result['refresh_error'], code)
                self.assertFalse(client.failure_guard.path.exists())
                self.assertNotIn('PRIVATE DETAIL', json.dumps(result))

    def test_team_security_failure_stays_hard_and_quota_remains_fail_closed(self):
        connection, workspace, client = self.public()
        client.search(public_fixture.query(), consent=True); self.now[0] += 601
        with patch.object(self.http, 'json', side_effect=FetchError('tls_verification_failed')):
            with self.assertRaisesRegex(FetchError, 'team_unavailable'): client.search(public_fixture.query(), consent=True)
        self.now[0] += 601
        with patch.object(self.http, 'json', side_effect=FetchError('network_error')):
            with self.assertRaisesRegex(FetchError, 'team_unavailable'): client.search(public_fixture.query(), consent=True)
            with self.assertRaisesRegex(CrawlError, 'team_unavailable'): connection.reserve('liepin', 'request')
        self.assertEqual(client.failure_guard.read(self.now[0]), 'team_unavailable')

    def test_pac_configured_team_search_completes_original_report(self):
        connection, workspace, client = self.public()
        tasks = PublicTasks(workspace, hybrid_client=client); self.addCleanup(tasks.close)
        pac = Mock(); policy = NetworkPolicy(pac=pac)
        with patch.object(workspace, 'network_policy', return_value=policy):
            tasks.search({'consent': True, 'query': public_fixture.query().payload()})
            tasks._thread.join(20)
        self.assertFalse(tasks._thread.is_alive())
        state = tasks.snapshot(); self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(workspace.status()['counts']['full_text'], 2)
        self.assertTrue(workspace.report(state['report_id'])['manifest'])
        # The remote connection owns its policy; local worker stop bindings
        # must not leak into a connection shared with later remote queries.
        pac.bind_cancellation.assert_not_called()

    def test_cancelled_pac_team_search_does_not_create_report_after_response(self):
        connection, workspace, client = self.public()
        tasks = PublicTasks(workspace, hybrid_client=client); self.addCleanup(tasks.close)
        entered = threading.Event(); release = threading.Event(); original = self.http.json
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(10): raise AssertionError('fixture was not released')
            return original(*args, **kwargs)
        with patch.object(workspace, 'network_policy', return_value=NetworkPolicy(pac=Mock())), patch.object(self.http, 'json', side_effect=blocked):
            try:
                accepted = tasks.search({'consent': True, 'query': public_fixture.query().payload()})
                self.assertTrue(entered.wait(3), tasks.snapshot())
                tasks.cancel({'id': accepted['id']})
            finally:
                release.set(); tasks._thread.join(20)
        self.assertFalse(tasks._thread.is_alive())
        self.assertEqual(tasks.snapshot()['status'], 'cancelled')
        self.assertEqual(workspace.status()['counts']['full_text'], 0)
        self.assertFalse(workspace.status()['runs'])

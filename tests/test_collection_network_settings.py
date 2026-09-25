"""Workspace consent reaches every advanced route and cached client, including CLI."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector, main
from vibe_job_radar.network import Response, SafeHTTP, SiteFetcher
from vibe_job_radar.network_policy import current_policy, use_policy, NetworkPolicy
from vibe_job_radar.workspace import Workspace, InputError


class CollectionNetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name)); self.collector = Collector(self.workspace)
        env = patch('urllib.request.getproxies', return_value={}); env.start(); self.addCleanup(env.stop)

    def preference(self, enabled=True):
        settings = self.workspace.network_state()
        self.workspace.network_preferences({'mode':'fake_ip_doh' if enabled else 'system',
            'revision':settings['revision'], 'consent':enabled})

    def start(self, mode):
        return self.collector.start(dict(mode=mode, roles=['architect'], platforms=['boss'],
            permit_platforms=['boss'], consent=True, rights_note='Synthetic contract',
            urls='https://www.zhipin.com/job_detail/1.html\nhttps://www.zhipin.com/job_detail/2.html',
            endpoint='https://publisher.example/jobs', contract_ref='https://publisher.example/docs',
            search_storage_rights=True, search_budget=2, detail_budget=2))

    def check(self, transport, enabled=True):
        self.assertEqual(transport.network_policy.encrypted_dns, enabled)
        self.assertIs(transport.resolver, self.workspace.dns_resolver)
        self.assertIs(transport.network_policy, current_policy())

    def test_all_three_routes_bind_workspace_before_constructing_clients(self):
        self.preference()
        seen = []
        def provider(client, url, **kwargs):
            self.check(client); seen.append(url)
            return {'web':{'results':[]}, 'jobs':[], 'next_cursor':None}
        def detail(client, url):
            self.check(client.transport); seen.append(url)
            return Response(200, {}, b'<h1>synthetic incomplete detail</h1>', url)
        outer = NetworkPolicy.capture()
        with use_policy(outer), patch.object(SafeHTTP, 'json', provider), patch.object(SiteFetcher, 'fetch', detail):
            for mode in ('search', 'urls', 'feed'):
                state = self.start(mode)
                self.collector.step({'id':state['id'], 'api_key':'synthetic-key'})
                self.assertIs(current_policy(), outer)
        self.assertEqual(len(seen), 3)

    def test_cached_search_client_updates_consent_without_resetting_pacing_or_denials(self):
        self.preference(); state = self.start('search'); clients = []
        def provider(client, url, **kwargs):
            clients.append(client)
            self.check(client, enabled=len(clients)==1)
            if len(clients) == 1:
                client.last_request['api.search.brave.com'] = 123
                client.blocked_hosts.add('denied.example')
            return {'web':{'results':[]}}
        with patch.object(SafeHTTP, 'json', provider):
            self.collector.step({'id':state['id'], 'api_key':'synthetic-key'})
            self.preference(False)
            self.collector.step({'id':state['id'], 'api_key':'synthetic-key'})
        self.assertIs(clients[0], clients[1])
        self.assertEqual(clients[1].last_request, {'api.search.brave.com':123})
        self.assertIn('denied.example', clients[1].blocked_hosts)

    def test_cached_detail_client_keeps_robots_and_network_refusals_after_preference_change(self):
        self.preference(); state = self.start('urls'); clients = []; rules = object()
        def detail(client, url):
            clients.append(client); self.check(client.transport, enabled=len(clients)==1)
            if len(clients) == 1:
                client.robots['fixture'] = rules
                client.transport.blocked_hosts.add('denied.example')
                client.transport.last_request['www.zhipin.com'] = 123
            return Response(200, {}, b'<h1>synthetic incomplete detail</h1>', url)
        with patch.object(SiteFetcher, 'fetch', detail):
            self.collector.step({'id':state['id']})
            self.preference(False)
            self.collector.step({'id':state['id']})
        self.assertIs(clients[0], clients[1])
        self.assertIs(clients[1].robots['fixture'], rules)
        self.assertIn('denied.example', clients[1].transport.blocked_hosts)
        self.assertEqual(clients[1].transport.last_request['www.zhipin.com'], 123)

    def test_feed_continuation_uses_current_consent_on_same_client(self):
        self.preference(); state = self.start('feed'); clients = []
        def provider(client, url, **kwargs):
            clients.append(client); self.check(client, enabled=len(clients)==1)
            return {'jobs':[], 'next_cursor':'next' if len(clients)==1 else None}
        with patch.object(SafeHTTP, 'json', provider):
            self.collector.step({'id':state['id']})
            self.preference(False)
            result = self.collector.step({'id':state['id']})
        self.assertIs(clients[0], clients[1]); self.assertEqual(result['feed_requests'], 2)

    def test_corrupt_preferences_fail_before_any_attempt_or_inflight_marker(self):
        for mode in ('search', 'urls', 'feed'):
            state = self.start(mode); path = self.collector._path(state['id']); before = path.read_bytes()
            (self.workspace.root/'network-preferences.json').write_text('{broken', encoding='utf-8')
            with self.subTest(mode=mode), patch.object(SafeHTTP, 'request', side_effect=AssertionError('no request')):
                with self.assertRaises(InputError): self.collector.step({'id':state['id'], 'api_key':'synthetic'})
            self.assertEqual(path.read_bytes(), before)

    def test_report_generation_remains_offline_with_broken_network_preferences(self):
        state = self.start('urls'); state['phase'] = 'report'; self.collector._save(state)
        (self.workspace.root/'network-preferences.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(self.collector.step({'id':state['id']})['status'], 'needs_attention')

    def test_cli_reads_saved_consent_without_workbench_request_context(self):
        self.preference(); state = self.start('feed'); seen = []
        def provider(client, url, **kwargs):
            self.assertTrue(client.network_policy.encrypted_dns); seen.append(url)
            return {'jobs':[], 'next_cursor':None}
        with patch.object(SafeHTTP, 'json', provider), patch('builtins.print'):
            self.assertEqual(main(['--workspace', str(self.workspace.root), '--run-id', state['id']]), 2)
        self.assertEqual(seen, ['https://publisher.example/jobs'])

    def test_another_workspace_does_not_inherit_consent(self):
        self.preference()
        with tempfile.TemporaryDirectory() as path:
            other = Workspace(path)
            with use_policy(self.workspace.network_policy()):
                self.assertFalse(other.network_policy().encrypted_dns)

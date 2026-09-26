"""No real account or provider request: exercise queue privacy and login policy."""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import Registry, builtins
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.password_login import LoginCredentials
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import InputError, Workspace
from test_automatic_collection import MemoryBackend


ADAPTER = builtins().get('liepin')
USERNAME = 'synthetic-user@example.test'
PASSWORD = 'synthetic-password-not-a-real-account'
API = 'https://api-passport.liepin.com/api/com.liepin.passport.account.'


class PasswordContractTests(unittest.TestCase):
    def test_only_explicit_authentication_allows_password_post(self):
        contract = contract_for(ADAPTER)
        url = API + 'account-pwd-login'
        with self.assertRaises(CrawlError):
            contract.match(url, 'POST', 'XHR')
        rule = contract.match(url, 'POST', 'XHR', authentication=True)
        self.assertEqual(rule.role, 'login')  # Never observe/retain login responses as job data.
        rule.validate_headers('POST', {'Origin': 'https://www.liepin.com'})
        with self.assertRaises(CrawlError):
            rule.validate_headers('POST', {'Origin': 'https://outside.example.test'})
        for suffix in ('account-pwd-register', 'logout', 'set-init-pwd', 'tel-sms-login'):
            with self.subTest(suffix=suffix), self.assertRaises(CrawlError):
                contract.match(API + suffix, 'POST', 'XHR', authentication=True)

    def test_preflight_cannot_grant_other_methods_or_headers(self):
        rule = contract_for(ADAPTER).match(API + 'account-pwd-login', 'OPTIONS', 'Preflight', authentication=True)
        headers = {'Origin': 'https://www.liepin.com', 'Access-Control-Request-Method': 'POST',
                   'Access-Control-Request-Headers': 'content-type,x-client-type'}
        rule.validate_headers('OPTIONS', headers)
        for change in ({'Access-Control-Request-Method': 'DELETE'},
                       {'Access-Control-Request-Headers': 'authorization'}):
            with self.assertRaises(CrawlError): rule.validate_headers('OPTIONS', {**headers, **change})

    def test_credentials_are_explicit_bounded_and_do_not_appear_in_repr(self):
        data = {'username': USERNAME, 'password': PASSWORD, 'credential_consent': True}
        value = LoginCredentials.from_input(data)
        self.assertNotIn(USERNAME, repr(value)); self.assertNotIn(PASSWORD, repr(value))
        value.clear(); self.assertEqual((value.username, value.password), ('', ''))
        for change in ({'credential_consent': False}, {'credential_consent': 1},
                       {'password': ''}, {'password': None}, {'password': 'p'*257},
                       {'username': '\nname'}, {'username': ' '}):
            with self.subTest(change=change), self.assertRaises(InputError):
                LoginCredentials.from_input({**data, **change})


class PasswordServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(Path(self.tmp.name))
        self.service = GuidedService(self.workspace, registry=Registry([ADAPTER]), backend_factory=MemoryBackend)
        self.addCleanup(self.service.close)
        self.service._submit = Mock()
        self.ident = self.service.create({'platform': 'liepin', 'keyword': '时间序列',
            'consent': True, 'rights_note': 'synthetic login test', 'auto_collect': True,
            'max_pages': 1, 'max_jobs': 1})['id']

    def action(self, **extra):
        return self.service.action({'id': self.ident, 'action': 'login_password',
            'username': USERNAME, 'password': PASSWORD, 'credential_consent': True, **extra})

    def test_only_queue_receives_credentials_and_state_contains_no_secrets(self):
        self.action()
        action, ident, secret = self.service._submit.call_args.args
        self.assertEqual((action, ident), ('login_password', self.ident))
        self.assertEqual((secret.username, secret.password), (USERNAME, PASSWORD))
        state = self.service._load(self.ident)
        self.assertTrue(state['auto_continue_after_login'])
        public = json.dumps(self.service.state()) + self.service._path(self.ident).read_text(encoding='utf-8')
        self.assertNotIn(USERNAME, public); self.assertNotIn(PASSWORD, public)
        secret.clear()

    def test_rejected_input_does_not_mutate_task(self):
        before = self.service._path(self.ident).read_bytes()
        for changes in ({'credential_consent': False}, {'cookie': 'synthetic'},
                        {'auto_continue': True}, {'url': 'https://outside.example.test'}):
            with self.subTest(changes=changes), self.assertRaises(InputError): self.action(**changes)
            self.assertEqual(self.service._path(self.ident).read_bytes(), before)

    def test_password_action_stays_on_original_query_and_arms_return(self):
        self.action()
        secret = self.service._submit.call_args.args[2]
        backend = MemoryBackend(); backend.password_login = Mock(side_effect=lambda value: (value.clear(), 'login_password_submitted')[1])
        self.service._backends[self.ident] = backend
        state = self.service._load(self.ident)
        self.service._run('login_password', state, secret)
        self.assertEqual(backend.calls, [(state['search_url'], True)])
        backend.password_login.assert_called_once_with(secret)
        self.assertEqual(state['status'], 'waiting_manual')
        self.assertEqual(state['authentication'], 'manual_pending')
        self.assertEqual(state['login_continuation'], 'watching')
        self.assertEqual(secret.password, '')

        # The same task proceeds from two stable readable observations to its
        # existing bounded collector and report, without another login call.
        self.service._login_return.interval = 0
        self.service._login_return.tick(self.service)
        self.service._login_return.tick(self.service)
        action, ident = self.service._submit.call_args.args
        self.assertEqual((action, ident), ('capture', self.ident))
        state = self.service._load(self.ident)
        self.service._run(action, state, None)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['outcome']['saved'], 1)
        self.assertTrue(state['report_id'])
        backend.password_login.assert_called_once()

    def test_unchecked_agreement_handoff_keeps_original_task_watching(self):
        self.action()
        secret = self.service._submit.call_args.args[2]
        backend = MemoryBackend()
        backend.password_login = Mock(side_effect=lambda value: (value.clear(), 'login_agreement_required')[1])
        self.service._backends[self.ident] = backend
        state = self.service._load(self.ident)
        self.service._run('login_password', state, secret)
        self.assertEqual(state['code'], 'login_agreement_required')
        self.assertEqual(state['status'], 'waiting_manual')
        self.assertEqual(state['authentication'], 'manual_pending')
        self.assertEqual(state['login_continuation'], 'watching')
        self.assertFalse(state.get('report_id'))
        self.assertEqual(backend.calls, [(state['search_url'], True)])
        self.assertEqual(secret.password, '')
        public = json.dumps(self.service.state()) + self.service._path(self.ident).read_text(encoding='utf-8')
        self.assertNotIn(USERNAME, public); self.assertNotIn(PASSWORD, public)
        backend.password_login.assert_called_once()

    def test_opted_in_manual_login_also_preserves_query(self):
        self.service.action({'id': self.ident, 'action': 'login', 'auto_continue': True})
        state = self.service._load(self.ident)
        backend = MemoryBackend(); self.service._backends[self.ident] = backend
        self.service._run('login', state, None)
        self.assertEqual(backend.calls, [(state['search_url'], True)])

    def test_other_platform_retains_its_separate_login_entry(self):
        adapter = builtins().get('boss')
        self.service.registry.register(adapter)
        ident = self.service.create({'platform': 'boss', 'keyword': '时间序列',
            'consent': True, 'rights_note': 'synthetic separate login entry'})['id']
        self.service.action({'id': ident, 'action': 'login', 'auto_continue': True})
        state = self.service._load(ident)
        backend = MemoryBackend(); self.service._backends[ident] = backend
        self.service._run('login', state, None)
        self.assertEqual(backend.calls, [(adapter.login_url, True)])

    def test_custom_liepin_origin_keeps_its_declared_login_entry(self):
        adapter = replace(ADAPTER, login_url='https://www.liepin.com/custom-login')
        self.service.registry = Registry([adapter])
        self.service.action({'id': self.ident, 'action': 'login', 'auto_continue': True})
        state = self.service._load(self.ident)
        backend = MemoryBackend(); self.service._backends[self.ident] = backend
        self.service._run('login', state, None)
        self.assertEqual(backend.calls, [(adapter.login_url, True)])

    def test_worker_discards_credentials_even_if_navigation_fails(self):
        original_submit = GuidedService._submit.__get__(self.service)
        self.service._submit = original_submit
        captured = []
        def fail(action, state, secret):
            captured.append(secret)
            raise CrawlError('robots_denied')
        self.service._run = fail
        self.action()
        deadline = time.monotonic() + 5
        while self.service.state()['busy'] and time.monotonic() < deadline: time.sleep(.01)
        self.assertFalse(self.service.state()['busy'])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].password, '')
        self.assertEqual(self.service._load(self.ident)['code'], 'robots_denied')
        self.assertTrue(self.service._queue.empty())

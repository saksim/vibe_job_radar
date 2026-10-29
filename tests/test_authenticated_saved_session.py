"""Artificial cookies/credentials only; no proxy or recruitment-site requests."""
import base64
from dataclasses import replace
from contextlib import closing
import hmac
import sqlite3
import subprocess
import sys
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar import proxy_credentials
from vibe_job_radar.proxy_credentials import ProxyCredentials
from vibe_job_radar.loopback_proxy import LoopbackProxy
from vibe_job_radar.loopback_socks import LoopbackSocks5
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.guided import service as service_module
from vibe_job_radar.guided import saved_session as session_module
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.saved_session import SavedSession, MAX_AGE
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.workspace import Workspace
from test_guided import fixture_adapter

COOKIE=dict(name='fixture_session',value='ARTIFICIAL-COOKIE-NOT-A-REAL-ACCOUNT',
            domain='jobs.fixture.test',path='/',expires=-1,httpOnly=True,secure=True,sameSite='Lax')
USER='ARTIFICIAL-PROXY-USER'
PASSWORD='ARTIFICIAL-PROXY-SECRET'

def policy(process_key, *, password=PASSWORD):
    with patch.object(proxy_credentials,'_BINDING_KEY',process_key):
        credentials=ProxyCredentials(USER,password)
    return NetworkPolicy(source='explicit_http',
                         proxy=LoopbackProxy('127.0.0.1',18901,credentials))


class AuthenticatedSessionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=2000000000
        self.adapter=fixture_adapter()

    def lease(self,root=None,*,network='fixture'):
        target=root or self.root
        target.mkdir(parents=True,exist_ok=True)
        lease=SavedSession(target,self.adapter,backend='bridge',
                           browser='bundled',network=network,clock=lambda:self.now)
        self.addCleanup(lease.close)
        return lease

    def test_service_same_proxy_credentials_survive_new_process_binding_on_windows(self):
        first=policy(b'a'*32);second=policy(b'b'*32)
        self.assertNotEqual(first.fingerprint,second.fingerprint)
        service=GuidedService(Workspace(self.root),registry=Registry([self.adapter]))
        self.addCleanup(service.close);service._submit=Mock()
        created=service.create(dict(platform='fixture',keyword='fixture',roles=['time_series'],
                                   consent=True,rights_note='Synthetic test only',persist_session=True))
        state=service._load(created['id'])
        with patch.object(service_module,'current_policy',return_value=first):
            saved=service._new_session_lease(state)
            try:saved.save([COOKIE]);path=saved.path;before=path.read_bytes()
            finally:saved.close()
        backend=SimpleNamespace(adapter=self.adapter,error=None,auth_mode=False,close=Mock())
        service.factory=Mock(return_value=backend)
        with patch.object(service_module,'current_policy',return_value=second):
            if os.name=='nt':
                service._backend(state)
                self.assertEqual(service.factory.call_args.kwargs['storage_state']['cookies'],[COOKIE])
                self.assertEqual(state['authentication'],'restored_session_unverified')
                self.assertEqual(json.loads(before)['version'],2)
                for secret in (USER,PASSWORD,COOKIE['value']):self.assertNotIn(secret.encode(),before)
            else:
                # POSIX stays owner-only v1; no stable password verifier is saved.
                with self.assertRaises(CrawlError) as raised:service._backend(state)
                self.assertEqual(raised.exception.code,'saved_session_incompatible')
                service.factory.assert_not_called()
                self.assertEqual(json.loads(before)['version'],1)
        self.assertEqual(path.read_bytes(),before)

    def test_unreadable_expired_snapshot_is_preserved_until_explicit_forget(self):
        saved=self.lease();saved.save([COOKIE])
        data=json.loads(saved.path.read_bytes())
        data['payload']=base64.b64encode(b'not-a-valid-protected-or-json-payload').decode()
        saved.path.write_text(json.dumps(data),encoding='utf-8')
        before=saved.path.read_bytes();self.now+=MAX_AGE+1
        with self.assertRaises(CrawlError):saved.restore()
        self.assertEqual(saved.path.read_bytes(),before)
        saved.forget();self.assertFalse(saved.path.exists())

    def test_all_automatic_writes_preserve_a_mismatched_existing_snapshot(self):
        for i,cookies in enumerate(([COOKIE],[],[{**COOKIE,'domain':'unrelated.invalid'}])):
            with self.subTest(case=i):
                root=self.root/str(i);saved=self.lease(root,network='first')
                saved.save([COOKIE]);before=saved.path.read_bytes();saved.close()
                different=self.lease(root,network='different')
                with self.assertRaises(CrawlError) as raised:different.save(cookies)
                self.assertEqual(raised.exception.code,'saved_session_incompatible')
                self.assertEqual(different.path.read_bytes(),before)


class AuthenticatedV2StorageTests(unittest.TestCase):
    """Exercise v2 on every OS with an opaque, entropy-bound test codec.

    The independent process test below uses the real Windows DPAPI instead.
    This model makes schema/mutation failures testable on POSIX, without
    asserting that the test codec itself encrypts or protects secrets.
    """
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 2_000_000_000
        self.adapter = fixture_adapter()
        self.policy = policy(b'a' * 32)
        self.protected = {}
        codec = patch.object(session_module, '_dpapi', side_effect=self.codec)
        self.crypto = codec.start()
        self.addCleanup(codec.stop)

    def codec(self, data, entropy, *, decrypt=False):
        if decrypt:
            saved = self.protected.get(data)
            if saved is None or not hmac.compare_digest(saved[1], entropy):
                raise CrawlError('saved_session_unreadable')
            return saved[0]
        token = ('artificial-opaque-blob-' + str(len(self.protected))).encode()
        self.protected[token] = (data, entropy)
        return token

    def lease(self, *, selected=None, root=None, adapter=None, backend='bridge', browser='bundled'):
        selected = selected or self.policy
        root = root or self.root
        root.mkdir(parents=True, exist_ok=True)
        lease = SavedSession(root, adapter or self.adapter, backend=backend, browser=browser,
                             network=selected._saved_session_network(), clock=lambda: self.now,
                             _credentials=selected.proxy.credentials)
        self.addCleanup(lease.close)
        return lease

    def installed(self, lease, content):
        lease.path.write_bytes(content)
        lease.path.chmod(0o600)

    def assert_preserved(self, lease, original, *, code=None):
        actions = (lease.restore, lambda: lease.save([COOKIE]), lambda: lease.save([]),
                   lambda: lease.save([{**COOKIE, 'domain': 'unrelated.invalid'}]))
        for action in actions:
            with self.assertRaises(CrawlError) as raised:
                action()
            if code is not None:
                self.assertEqual(raised.exception.code, code)
            self.assertEqual(lease.path.read_bytes(), original)
            self.assertFalse(list(lease.root.glob('.session-*')))

    def test_private_context_is_stable_but_public_policy_ids_remain_process_scoped(self):
        other = policy(b'b' * 32)
        self.assertNotEqual(self.policy.fingerprint, other.fingerprint)
        self.assertEqual(self.policy._saved_session_network(), other._saved_session_network())
        lease = self.lease()
        lease.save([COOKIE])
        original = lease.path.read_bytes()
        envelope = json.loads(original)
        self.assertEqual(set(envelope), {'version', 'binding', 'saved_at', 'protection', 'payload'})
        protected, entropy = self.protected[base64.b64decode(envelope['payload'])]
        self.assertEqual(json.loads(protected), {'cookies': [COOKIE], 'saved_at': self.now})
        for forbidden in (USER.encode(), PASSWORD.encode(), self.policy.proxy.credentials.binding.encode(),
                          self.policy.fingerprint.encode(), entropy, entropy.hex().encode()):
            self.assertNotIn(forbidden, original)
        lease.close()
        reopened = self.lease(selected=other)
        self.assertEqual(reopened.restore(), {'cookies': [COOKIE], 'origins': []})
        self.assertEqual(reopened.status, 'restored_unverified')
        self.assertEqual(reopened.path.read_bytes(), original)

    def test_password_and_username_changes_cannot_read_replace_or_delete_even_after_expiry(self):
        saved = self.lease()
        saved.save([COOKIE])
        original = saved.path.read_bytes()
        saved.close()
        for user, secret in ((USER, PASSWORD + '-changed'), (USER + '-changed', PASSWORD)):
            with self.subTest(changed_user=user != USER):
                selected = replace(self.policy, proxy=LoopbackProxy('127.0.0.1', 18901,
                                                                    ProxyCredentials(user, secret)))
                changed = self.lease(selected=selected)
                self.now += MAX_AGE + 1
                self.assert_preserved(changed, original, code='saved_session_unreadable')
                changed.close()
        correct = self.lease()
        self.assertIsNone(correct.restore())
        self.assertEqual(correct.status, 'expired')
        self.assertFalse(correct.path.exists())

    def test_every_noncredential_route_component_remains_bound(self):
        saved = self.lease()
        saved.save([COOKIE])
        original = saved.path.read_bytes()
        saved.close()
        c = self.policy.proxy.credentials
        variants = (
            replace(self.policy, source='other'),
            replace(self.policy, proxy=LoopbackProxy('::1', 18901, c)),
            replace(self.policy, proxy=LoopbackProxy('127.0.0.1', 18902, c)),
            replace(self.policy, proxy=LoopbackSocks5('127.0.0.1', 18901, c)),
            replace(self.policy, bypass=('jobs.fixture.test',)),
            replace(self.policy, error='fixture-refusal'),
            replace(self.policy, encrypted_dns=True),
            replace(self.policy, encrypted_dns=True, public_dns=True),
            replace(self.policy, pac_url_mode=True),
            replace(self.policy, pac=SimpleNamespace(binding='fixture-pac'), pac_id='fixture-content'),
        )
        for selected in variants:
            with self.subTest(selected=selected):
                changed = self.lease(selected=selected)
                self.assert_preserved(changed, original, code='saved_session_incompatible')
                changed.close()
        pac = replace(self.policy, pac=SimpleNamespace(binding='fixture-pac'), pac_id='fixture-content')
        self.assertNotEqual(pac._saved_session_network(),
                            replace(pac, pac_id='new-content')._saved_session_network())
        self.assertNotEqual(pac._saved_session_network(),
                            replace(pac, pac_route_host='other.fixture.test')._saved_session_network())

    def test_workspace_platform_adapter_backend_and_browser_cannot_share_cookies(self):
        saved = self.lease()
        saved.save([COOKIE])
        original = saved.path.read_bytes()
        saved.close()
        variants = ({'root': self.root / 'other'}, {'adapter': replace(self.adapter, key='another')},
                    {'adapter': replace(self.adapter, version='different')},
                    {'backend': 'native'}, {'browser': 'chrome'})
        for options in variants:
            with self.subTest(options=tuple(options)):
                changed = self.lease(**options)
                self.installed(changed, original)
                self.assert_preserved(changed, original, code='saved_session_incompatible')
                changed.close()

    def test_public_metadata_and_ciphertext_tampering_never_authorize_automatic_mutation(self):
        saved = self.lease()
        saved.save([COOKIE])
        envelope = json.loads(saved.path.read_bytes())
        variants = ({**envelope, 'version': 1}, {**envelope, 'version': True},
                    {**envelope, 'saved_at': self.now - MAX_AGE - 1},
                    {**envelope, 'unexpected': 'metadata'},
                    {**envelope, 'protection': 'owner_only'},
                    {**envelope, 'payload': base64.b64encode(b'corrupt').decode()},
                    {**envelope, 'binding': {**envelope['binding'], 'browser': 'different'}})
        for data in variants:
            with self.subTest(field=[k for k in data if data[k] != envelope.get(k)]):
                original = json.dumps(data).encode()
                self.installed(saved, original)
                self.assert_preserved(saved, original)
        saved.forget()
        self.assertFalse(saved.path.exists())

    def test_version_one_instance_cannot_downgrade_even_metadata_edited_version_two(self):
        saved = self.lease()
        saved.save([COOKIE])
        original = saved.path.read_bytes()
        envelope = json.loads(original)
        saved.close()
        legacy = SavedSession(self.root, self.adapter, backend='bridge', browser='bundled',
                              network=self.policy.fingerprint, clock=lambda: self.now)
        self.addCleanup(legacy.close)
        self.assert_preserved(legacy, original, code='saved_session_incompatible')
        # Even editing all public v1 metadata cannot make v2 protected data
        # readable using v1 entropy, and expiry must not bypass that check.
        envelope.update(version=1, binding=legacy.binding, saved_at=self.now - MAX_AGE - 1,
                        protection=legacy._protection)
        edited = json.dumps(envelope).encode()
        self.installed(legacy, edited)
        self.assert_preserved(legacy, edited)
        legacy.forget()
        self.assertFalse(legacy.path.exists())

    def test_authenticated_v1_has_no_automatic_migration_to_v2(self):
        legacy = SavedSession(self.root, self.adapter, backend='bridge', browser='bundled',
                              network=self.policy.fingerprint, clock=lambda: self.now)
        try:
            legacy.save([COOKIE])
            original = legacy.path.read_bytes()
        finally:
            legacy.close()
        saved = self.lease()
        self.assert_preserved(saved, original, code='saved_session_incompatible')
        saved.forget()
        saved.save([COOKIE])
        self.assertEqual(json.loads(saved.path.read_bytes())['version'], 2)

    def test_decrypted_payload_schema_and_timestamp_are_validated_before_deletion(self):
        saved = self.lease()
        saved.save([COOKIE])
        envelope = json.loads(saved.path.read_bytes())
        valid = {'cookies': [COOKIE], 'saved_at': self.now}
        for payload in ([COOKIE], {**valid, 'saved_at': True}, {**valid, 'saved_at': self.now - 1},
                        {**valid, 'verifier': 'not-permitted'}, {**valid, 'cookies': [{}]}):
            with self.subTest(shape=type(payload).__name__):
                modified = dict(envelope, payload=base64.b64encode(
                    self.codec(json.dumps(payload).encode(), saved.entropy)).decode())
                original = json.dumps(modified).encode()
                self.installed(saved, original)
                self.assert_preserved(saved, original)
        saved.forget()

    def test_valid_expiry_empty_logout_and_cookie_filtering_keep_normal_semantics(self):
        for case in ('age', 'empty', 'foreign', 'cookie_expiry'):
            with self.subTest(case=case):
                saved = self.lease(root=self.root / case)
                cookie = {**COOKIE, 'expires': self.now + 1} if case == 'cookie_expiry' else COOKIE
                saved.save([cookie])
                if case == 'age':
                    self.now += MAX_AGE + 1
                    self.assertIsNone(saved.restore())
                    self.assertEqual(saved.status, 'expired')
                elif case == 'cookie_expiry':
                    self.now += 1
                    self.assertIsNone(saved.restore())
                    self.assertEqual(saved.status, 'empty')
                else:
                    saved.save([] if case == 'empty' else [{**COOKIE, 'domain': 'unrelated.invalid'}])
                    self.assertEqual(saved.status, 'empty')
                self.assertFalse(saved.path.exists())
                saved.save([COOKIE])
                self.assertEqual(saved.restore()['cookies'], [COOKIE])
                saved.close()


def child_probe():
    """Actual process/DPAPI, with artificial secrets defined only in this test."""
    options = json.loads(sys.stdin.read())
    selected = NetworkPolicy(source='explicit_application',
        proxy=LoopbackProxy('127.0.0.1', 18901, ProxyCredentials(USER, options.get('password', PASSWORD))))
    result = {'policy_id': selected.fingerprint}
    try:
        saved = SavedSession.for_policy(options['root'], fixture_adapter(), backend='bridge',
                                        browser='bundled', policy=selected, clock=lambda: 2_000_000_000)
        try:
            if options.get('action') == 'forget':
                saved.forget()
            elif options.get('action') == 'empty':
                saved.save([])
            else:
                restored = saved.restore()
                result['cookies_match'] = restored == {'cookies': [COOKIE], 'origins': []}
            result['status'] = saved.status
        finally:
            saved.close()
    except CrawlError as error:
        result['error'] = error.code
    print(json.dumps(result))


class AuthenticatedProcessAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.adapter = fixture_adapter()

    def test_actual_process_lock_and_restart_keep_native_platform_storage_contract(self):
        selected = NetworkPolicy(source='explicit_application',
            proxy=LoopbackProxy('127.0.0.1', 18901, ProxyCredentials(USER, PASSWORD)))
        saved = SavedSession.for_policy(self.root, self.adapter, backend='bridge', browser='bundled',
                                        policy=selected, clock=lambda: 2_000_000_000)
        self.addCleanup(saved.close)
        saved.save([COOKIE])
        original = saved.path.read_bytes()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(
            [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]))
        def child(**options):
            response = subprocess.run([sys.executable, '-c',
                'from test_authenticated_saved_session import child_probe; child_probe()'],
                input=json.dumps({'root': str(self.root), **options}), env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=20, check=True)
            for secret in (USER, PASSWORD, COOKIE['value']):
                self.assertNotIn(secret, response.stdout + response.stderr)
            return json.loads(response.stdout)
        self.assertEqual(child()['error'], 'saved_session_busy')
        saved.close()
        restored = child()
        self.assertNotEqual(restored['policy_id'], selected.fingerprint)
        if os.name == 'nt':
            self.assertEqual(restored['status'], 'restored_unverified')
            self.assertTrue(restored['cookies_match'])
            self.assertNotIn(COOKIE['value'].encode(), original)
        else:
            self.assertEqual(restored['error'], 'saved_session_incompatible')
            self.assertEqual(json.loads(original)['protection'], 'owner_only')
            self.assertEqual(saved.path.stat().st_mode & 0o777, 0o600)
        wrong = child(password=PASSWORD + '-changed', action='empty')
        self.assertEqual(wrong['error'], 'saved_session_unreadable' if os.name == 'nt'
                         else 'saved_session_incompatible')
        self.assertEqual(saved.path.read_bytes(), original)
        self.assertEqual(child(password=PASSWORD + '-changed', action='forget')['status'], 'cleared')
        self.assertFalse(saved.path.exists())

    def service(self, *, native=False, persist=True):
        if native:
            from test_native_acquisition import adapter
            self.adapter = adapter()
        service = GuidedService(Workspace(self.root), registry=Registry([self.adapter]))
        self.addCleanup(service.close)
        service._submit = Mock()
        job = service.create(dict(platform='fixture', keyword='fixture', roles=['time_series'],
                                  consent=True, rights_note='Synthetic test only', persist_session=persist,
                                  backend='native' if native else 'bridge', native_consent=native))
        return service, service._load(job['id'])

    def seed(self, service, state, selected):
        with patch.object(service_module, 'current_policy', return_value=selected):
            saved = service._new_session_lease(state)
            try:
                saved.save([COOKIE])
                return saved.path, saved.path.read_bytes()
            finally:
                saved.close()

    def test_native_handoff_and_live_policy_invalidation_remain_separate_from_disk_binding(self):
        service, state = self.service(native=True)
        first = policy(b'a' * 32)
        second = policy(b'b' * 32)
        path, original = self.seed(service, state, first)
        backend = SimpleNamespace(adapter=self.adapter, error=None, auth_mode=False, close=Mock(),
                                  wire=SimpleNamespace(network_policy=second))
        service.native_factory = Mock(return_value=backend)
        with patch.object(service_module, 'current_policy', return_value=second):
            if os.name == 'nt':
                service._backend(state)
                self.assertEqual(service.native_factory.call_args.kwargs['storage_state']['cookies'], [COOKIE])
                with patch.object(service_module, 'current_policy', return_value=first):
                    with self.assertRaises(CrawlError) as error:
                        service._backend(state)
                    self.assertEqual(error.exception.code, 'native_policy_changed')
                backend.close.assert_called_once()
            else:
                with self.assertRaises(CrawlError):
                    service._backend(state)
                service.native_factory.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_restore_failure_preserves_search_and_explicit_login_quota_order(self):
        service, state = self.service()
        service.ledger.limits=replace(service.ledger.limits,login_interval=300,logins_day=3)
        first = policy(b'a' * 32)
        path, original = self.seed(service, state, first)
        wrong = policy(b'a' * 32, password=PASSWORD + '-changed')
        service.factory = Mock()
        def visits():
            with closing(sqlite3.connect(service.ledger.path)) as conn:
                return conn.execute('SELECT site,kind FROM visits').fetchall()
        before = visits()
        with patch.object(service_module, 'current_policy', return_value=wrong):
            with self.assertRaises(CrawlError):
                service._run('search', state, None)
            self.assertEqual(visits(), before)
            with self.assertRaises(CrawlError):
                service._run('login', state, None)
            self.assertEqual(visits(), before + [('fixture', 'login')])
            with self.assertRaises(CrawlError) as error:
                service._run('login', state, None)
            self.assertEqual(error.exception.code, 'login_rate_limited')
            self.assertEqual(visits(), before + [('fixture', 'login')])
        service.factory.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_authenticated_policy_does_not_enable_default_persistence(self):
        service, state = self.service(persist=False)
        service.factory = Mock(return_value=SimpleNamespace(adapter=self.adapter, close=Mock()))
        with patch.object(service_module, 'current_policy', return_value=policy(b'a' * 32)):
            service._backend(state)
        self.assertFalse((self.root / '.radar-sessions').exists())
        self.assertNotIn('storage_state', service.factory.call_args.kwargs)


if __name__=='__main__':unittest.main()

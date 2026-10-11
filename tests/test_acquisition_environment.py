"""Authored local fixtures; no account, job-site request or live certification."""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from vibe_job_radar import runtime_identity as runtime
from vibe_job_radar.guided import environment_history as env, attempt_history
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.qualification import fingerprint
from vibe_job_radar.workspace import InputError
from vibe_job_radar.utils import atomic_json

ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-09-01T10:30:00+00:00'


def observed_runtime():
    return {'process_id': '1'*32, 'started_at': '2026-09-01T10:00:00+00:00',
            'python_version': '3.12.0', 'playwright_version': '1.63.0',
            'os': 'Windows', 'os_release': '11', 'freshness': 'unchanged',
            'source': {'kind': 'source_entry_snapshot', 'revision': 'a'*40,
                       'raw_sha256': 'b'*64, 'content_sha256': 'c'*64,
                       'content_algorithm': runtime.ALGORITHM, 'file_count': 1,
                       'executable_sha256': None}}


def backend(kind=NativeBackend, channel='chrome', version='157.0.1.0', policy=None):
    # Construct no browser: these objects exercise the observer's whitelist.
    value = object.__new__(kind)
    value.startup_report = {'browser_channel': channel}
    value.browser = types.SimpleNamespace(version=version)
    value.wire = types.SimpleNamespace(network_policy=policy or NetworkPolicy())
    return value


def task():
    return {'cards': [{'id': 'one'}], 'selection': ['one'], 'updated_at': NOW,
            env.KEY: env.new_history(checkpoint_at=NOW),
            attempt_history.KEY: attempt_history.new_history(checkpoint_at=NOW)}


def observe(state, owner, action='collect'):
    with patch.object(runtime, 'snapshot', return_value=observed_runtime()), patch.object(env, 'utc_now', return_value=NOW):
        return env.observe(state, owner, builtins().get('liepin'), action)


def start(state, observation):
    with patch.object(attempt_history, 'utc_now', return_value=NOW):
        a = attempt_history.begin(state, 'one')
    env.bind_attempt(state, a['sequence'], observation)
    return a


class RuntimeIdentityTests(unittest.TestCase):
    def test_source_identity_normalizes_text_but_not_binary_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'src').mkdir()
            file = root/'src/example.py'; file.write_bytes(b'x = 1\n')
            first = runtime.content_identity(root); raw = fingerprint(root)
            file.write_bytes(b'x = 1\r\n')
            self.assertEqual(first, runtime.content_identity(root))
            self.assertNotEqual(raw, fingerprint(root))
            binary = root/'src/data.bin'
            binary.write_bytes(b'EVIDENCE\0\n')
            first = runtime.content_identity(root); binary.write_bytes(b'EVIDENCE\0\r\n')
            self.assertNotEqual(first, runtime.content_identity(root))
            evidence = root/'examples/evidence/fixture.json'
            evidence.parent.mkdir(parents=True); evidence.write_bytes(b'{}\n')
            first = runtime.content_identity(root); evidence.write_bytes(b'{}\r\n')
            self.assertNotEqual(first, runtime.content_identity(root))
            cert = root/'tests/fixtures/connection_test_only.pem'
            cert.parent.mkdir(parents=True); cert.write_bytes(b'CERTIFICATE TEXT\n')
            first = runtime.content_identity(root); cert.write_bytes(b'CERTIFICATE TEXT\r\n')
            self.assertEqual(first, runtime.content_identity(root))

    def test_real_fresh_source_processes_bind_same_source_and_different_processes(self):
        code = ("import socket,json; socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('offline')); "
                "import vibe_job_radar.workbench; from vibe_job_radar.runtime_identity import snapshot; print(json.dumps(snapshot()))")
        values = []
        process_env = {**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        for _ in range(2):
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=process_env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout); runtime.validate(value); values.append(value)
        self.assertEqual(values[0]['freshness'], 'unchanged')
        self.assertEqual(values[0]['source']['content_sha256'], runtime.content_identity(ROOT)['sha256'])
        self.assertEqual(values[0]['source'], values[1]['source'])
        self.assertNotEqual(values[0]['process_id'], values[1]['process_id'])

    def test_late_bootstrap_never_attests_already_imported_services(self):
        with patch.object(runtime, '_STATE', None), patch.object(runtime, '_ROOT', None), patch.object(runtime, '_PORTABLE', None):
            runtime.bootstrap()  # This module already imported product services.
            first, second = runtime.snapshot(), runtime.snapshot()
            self.assertEqual(first['source']['kind'], 'unknown')
            self.assertEqual(first['process_id'], second['process_id'])

    def test_source_edit_is_changed_without_rewriting_original_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'README.md').write_text('original',encoding='utf8')
            original=observed_runtime(); original['source']=runtime.source_stamp(root)
            with patch.object(runtime,'_STATE',original),patch.object(runtime,'_ROOT',root),patch.object(runtime,'_PORTABLE',None):
                self.assertEqual(runtime.snapshot()['freshness'],'unchanged')
                (root/'README.md').write_text('modified',encoding='utf8')
                result=runtime.snapshot()
                self.assertEqual(result['freshness'],'changed')
                self.assertEqual(result['source'],original['source'])

    def test_portable_metadata_requires_actual_executable_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); exe=root/'test.exe'; exe.write_bytes(b'ARTIFICIAL BINARY')
            stamp=observed_runtime()['source']; stamp.update(kind='portable_build_manifest',
                executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
            atomic_json(root/'PORTABLE.json',{'runtime_source':stamp,'source':{'sha256':stamp['raw_sha256']}})
            with patch.object(sys,'executable',str(exe)):
                self.assertEqual(runtime.portable_stamp(root),stamp)
                exe.write_bytes(b'CHANGED')
                with self.assertRaises(ValueError):runtime.portable_stamp(root)


class EnvironmentHistoryTests(unittest.TestCase):
    def test_same_backend_reuse_retains_episode_and_new_browser_changes_it(self):
        first, second = task(), task(); owner=backend()
        observe(first,owner,'search'); observe(second,owner,'resume')
        self.assertEqual(first[env.KEY]['observations'][0]['session_id'],second[env.KEY]['observations'][0]['session_id'])
        observe(second,backend(channel='msedge'))
        self.assertNotEqual(second[env.KEY]['observations'][0]['session_id'],second[env.KEY]['observations'][1]['session_id'])
        self.assertEqual(second[env.KEY]['observations'][1]['browser_channel'],'msedge')

    def test_actual_bound_policy_is_used_without_resolving_or_evaluating_pac(self):
        class Pac:
            binding='ARTIFICIAL'; kind='domain'
            def evaluate(self,*_):raise AssertionError('must not evaluate')
        policy=NetworkPolicy(pac=Pac(),pac_id='PRIVATE-SCRIPT-ID',bypass=('private.internal',),encrypted_dns=True)
        with patch('socket.getaddrinfo',side_effect=AssertionError('no DNS')),patch.object(NetworkPolicy,'for_request',side_effect=AssertionError('no route request')):
            value=env.network(policy)
        self.assertEqual(value['transport'],'pac_by_host')
        self.assertEqual(value['dns'],'fake_ip_doh')
        self.assertEqual(value['policy_id'],policy.fingerprint)
        self.assertEqual(value['physical_route'],'unknown')
        encoded=json.dumps(value)
        self.assertNotIn('private.internal',encoded); self.assertNotIn('PRIVATE-SCRIPT-ID',encoded)
        state=task(); observe(state,backend(policy=policy))
        self.assertEqual(state[env.KEY]['observations'][0]['network'],value)

    def test_custom_backend_does_not_promote_claimed_browser_metadata(self):
        state=task(); owner=types.SimpleNamespace(startup_report={'browser_channel':'chrome'},
            browser=types.SimpleNamespace(version='123.0'),wire=types.SimpleNamespace(network_policy=NetworkPolicy()))
        observe(state,owner)
        item=state[env.KEY]['observations'][0]
        self.assertEqual(item['backend'],'unknown');self.assertEqual(item['browser_channel'],'unknown')
        self.assertEqual(item['network']['transport'],'unknown')

    def test_failed_and_unfinished_attempt_references_survive_json_restart(self):
        state=task(); seq=observe(state,backend()); a=start(state,seq)
        first=copy.deepcopy(state)
        with patch.object(attempt_history,'utc_now',return_value=NOW):
            attempt_history.finish(a,attempt_history.monotonic(),error=CrawlError('http_403'))
        start(state,seq)
        restored=json.loads(json.dumps(state))
        env.validate(restored);env.extends(first[env.KEY],restored[env.KEY])
        self.assertEqual(restored[attempt_history.KEY]['attempts'][0]['outcome'],'http_403')
        self.assertEqual(restored[attempt_history.KEY]['attempts'][1]['outcome'],'unfinished')
        self.assertEqual(restored[env.KEY]['attempts'],[{'attempt':1,'observation':1},{'attempt':2,'observation':1}])

    def test_legacy_and_old_writer_gaps_cannot_be_repaired_retroactively(self):
        state=task();del state[env.KEY];start(state,None)
        self.assertEqual(state[env.KEY]['origin'],'legacy_partial')
        self.assertIsNone(state[env.KEY]['attempts'][0]['observation'])
        observe(state,backend())
        self.assertIsNone(state[env.KEY]['attempts'][0]['observation'])
        fresh=task();fresh['updated_at']='2026-09-01T10:31:00+00:00'
        env.checkpoint(fresh,fresh['updated_at'],'2026-09-01T10:32:00+00:00')
        self.assertEqual(fresh[env.KEY]['origin'],'legacy_partial')

    def test_observation_and_reference_removal_or_rewrite_are_rejected(self):
        state=task();start(state,observe(state,backend()));original=copy.deepcopy(state[env.KEY])
        for change in ('delete','rewrite','missing'):
            changed=copy.deepcopy(original)
            if change=='delete':changed['attempts']=[]
            elif change=='rewrite':changed['observations'][0]['browser_version']='999.0'
            else:changed=None
            with self.subTest(change=change),self.assertRaises(CrawlError):env.extends(original,changed)

    def test_unknown_fields_bad_reference_and_limit_stop_without_truncation(self):
        state=task();start(state,observe(state,backend()))
        for case in ('extra','reference','chronology'):
            changed=copy.deepcopy(state)
            if case=='extra':changed[env.KEY]['observations'][0]['network']['credentials']='DO-NOT-KEEP'
            elif case=='reference':changed[env.KEY]['attempts'][0]['observation']=2
            else:changed[env.KEY]['observations'][0]['observed_at']='2026-09-01T11:00:00+00:00'
            with self.subTest(case=case),self.assertRaises(CrawlError):env.validate(changed)
        with patch.object(env,'MAX_OBSERVATIONS',1),self.assertRaises(CrawlError) as error:observe(state,backend())
        self.assertEqual(error.exception.code,'environment_history_limit')
        with patch.object(env,'MAX_OBSERVATIONS',1),self.assertRaises(CrawlError):env.require_capacity(state)
        self.assertEqual(len(state[env.KEY]['observations']),1)

    def test_content_browser_and_unknown_environment_are_distinct(self):
        state=task();start(state,observe(state,backend()))
        adapter=builtins().get('liepin')
        plan={'source_sha256':'b'*64,'source_content_sha256':'c'*64,'backend':'native','browser':'chrome',
              'browser_version':'157.0.1.0','platform':'liepin','adapter_version':adapter.version}
        result=env.assessment(plan,state[env.KEY],state[attempt_history.KEY])
        self.assertEqual(result['matching_detail_attempts'],1);self.assertEqual(result['gaps'],[])
        result=env.assessment({**plan,'source_content_sha256':'d'*64,'browser':'msedge'},state[env.KEY],state[attempt_history.KEY])
        self.assertEqual(result['mismatched_fields'],['browser','source'])
        state[env.KEY]['observations'][0]['runtime']['freshness']='changed'
        result=env.assessment(plan,state[env.KEY],state[attempt_history.KEY])
        self.assertIn('runtime_environment',result['gaps'])
        self.assertEqual(result['matching_detail_attempts'],0)

    def test_report_keeps_original_refs_and_rejects_missing_or_changed_copy(self):
        state=task();start(state,observe(state,backend()));report=copy.deepcopy(state)
        observe(state,backend(),'resume')
        env.require_report(state[env.KEY],report)
        for case in ('missing','changed','empty'):
            bad=copy.deepcopy(report)
            if case=='missing':del bad[env.KEY]
            elif case=='changed':bad[env.KEY]['observations'][0]['session_id']='f'*32
            else:bad[env.KEY]['attempts']=[]
            with self.subTest(case=case),self.assertRaises(CrawlError):env.require_report(state[env.KEY],bad)


class LedgerEnvironmentTests(unittest.TestCase):
    def fixture(self):
        import test_live_acceptance as fixtures
        value=fixtures.LiveAcceptanceTests()
        value.setUp();self.addCleanup(value.doCleanups)
        return value

    def recorded(self, fixture):
        fixture.plan=fixture.ledger.create({**fixture.data,'source_content_sha256':'c'*64,'browser_version':'157.0.1.0'})
        state=fixture.batch()
        owner=backend(channel=fixture.plan['browser'],version='157.0.1.0')
        with patch.object(runtime,'snapshot',return_value=observed_runtime()),patch.object(env,'utc_now',return_value=NOW):
            seq=env.observe(state,owner,fixture.adapter,'collect')
        row=state['cards'][0]
        with patch.object(attempt_history,'utc_now',return_value='2026-09-01T11:00:00+00:00'):
            attempt=attempt_history.begin(state,row['id']);env.bind_attempt(state,attempt['sequence'],seq)
            attempt_history.finish(attempt,attempt_history.monotonic(),record_id=row['record_id'])
        fixture.service._save(state)
        fixture.service._finalize_report(state,fixture.adapter)
        return state

    def test_ledger_copies_runtime_and_flags_scope_without_certifying(self):
        fixture=self.fixture();state=self.recorded(fixture)
        result=fixture.capture()
        row=result['runtime_environments'][state['id']]
        self.assertEqual(row['bound_detail_attempts'],1)
        self.assertEqual(row['matching_detail_attempts'],1)
        self.assertIn('physical_network_route_not_observed',result['gaps'])
        self.assertEqual(result['certification'],'not_live_verified')
        self.assertFalse(fixture.plan['threshold_approved'])

    def test_capture_cannot_drop_previously_seen_runtime_history(self):
        fixture=self.fixture();state=self.recorded(fixture);fixture.capture()
        del state[env.KEY];atomic_json(fixture.service._path(state['id']),state)
        with self.assertRaises(InputError):fixture.capture()

    def test_original_report_environment_cannot_be_replaced_by_current_process(self):
        fixture=self.fixture();state=self.recorded(fixture)
        folder=fixture.workspace.root/'reports'/state['report_id']
        audit=json.loads((folder/'guided_acquisition.json').read_bytes())
        self.assertEqual(audit[env.KEY]['observations'][0]['runtime']['process_id'],'1'*32)
        audit[env.KEY]['observations'][0]['runtime']['process_id']='2'*32
        atomic_json(folder/'guided_acquisition.json',audit)
        manifest=json.loads((folder/'run_manifest.json').read_bytes())
        manifest['output_files_sha256']['guided_acquisition.json']=hashlib.sha256((folder/'guided_acquisition.json').read_bytes()).hexdigest()
        atomic_json(folder/'run_manifest.json',manifest)
        result=fixture.capture()
        self.assertIn('invalid_item_evidence',result['gaps'])


class AcceptanceCLISourceTests(unittest.TestCase):
    def test_root_notes_are_allowed_but_uncommitted_or_ignored_payload_is_not(self):
        import contextlib
        import importlib.util
        import io
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'src').mkdir(); (root/'workspace').mkdir()
            (root/'src/example.py').write_text('x = 1',encoding='utf8')
            (root/'.gitignore').write_text('src/ignored.py',encoding='utf8')
            spec=importlib.util.spec_from_file_location('acceptance_cli_fixture',ROOT/'scripts/check_live_acceptance.py')
            cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
            command=['check_live_acceptance.py','--workspace',str(root/'workspace'),'plan',
                '--keyword','时间序列算法','--role','time_series','--selection-rule','ARTIFICIAL OFFLINE FIXTURE',
                '--timezone','UTC','--browser','chrome','--network-mode','system','--yes']
            dirty=['']
            def git_reply(args,**kwargs):
                # Authored Git metadata; real filesystem payload selection,
                # plan persistence and ignored-extra detection remain active.
                if 'rev-parse' in args:return 'a'*40
                if 'status' in args:
                    self.assertIn('--',args);self.assertIn('src',args)
                    return dirty[0]
                if 'ls-files' in args:return '.gitignore\0src/example.py\0'
                raise AssertionError('unexpected Git command')
            def run():
                with patch.object(cli,'ROOT',root),patch.object(sys,'argv',command), \
                        patch.object(cli.subprocess,'check_output',side_effect=git_reply),contextlib.redirect_stdout(io.StringIO()):
                    return cli.main()
            (root/'CONTINUE_HERE.md').write_text('keep original notes',encoding='utf8')
            (root/'tmp-generated.txt').write_text('not source',encoding='utf8')
            self.assertEqual(run(),0)
            plans=list((root/'workspace/.radar-acceptance').glob('*/plan.json'))
            self.assertEqual(len(plans),1)
            self.assertEqual(json.loads(plans[0].read_bytes())['schema_version'],2)
            dirty[0]=' M src/example.py'
            self.assertEqual(run(),1)
            dirty[0]=''
            (root/'src/ignored.py').write_text('uncommitted',encoding='utf8')
            self.assertEqual(run(),1)
            self.assertEqual(len(list((root/'workspace/.radar-acceptance').glob('*/plan.json'))),1)

if __name__ == '__main__':
    unittest.main()

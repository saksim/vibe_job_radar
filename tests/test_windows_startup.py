"""No real login registration: state transitions use an isolated registry double."""
import ctypes
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.windows_startup import CONSENT, MODE_CONSENT, WindowsStartup, StartupChanged, command_line
from vibe_job_radar.workspace import InputError, Workspace
from vibe_job_radar.workbench import LocalServer


class MemoryRun:
    def __init__(self):
        self.values = {}; self.writes = []; self.fail_create = False; self.fail_remove = False

    def read(self, name):
        return self.values.get(name)

    def create(self, name, command):
        if self.fail_create: raise PermissionError('PRIVATE OS MESSAGE')
        if name in self.values: raise StartupChanged('changed')
        self.values[name] = (1, command); self.writes.append(('create', name))

    def remove(self, name, command):
        if self.fail_remove: raise PermissionError('PRIVATE OS MESSAGE')
        if self.values.get(name) != (1, command): raise StartupChanged('changed')
        del self.values[name]; self.writes.append(('remove', name))


def fixture(root):
    workspace = Workspace(Path(root) / '工作区 with spaces')
    executable = Path(root).resolve() / '程序 with spaces' / 'VibeJobRadar.exe'
    executable.parent.mkdir(exist_ok=True); executable.write_bytes(b'fixture only, never executed')
    registry = MemoryRun()
    manager = WindowsStartup(workspace, executable=executable, registry=registry, portable=True, platform='win32')
    return workspace, executable, registry, manager


def enable(manager, **changes):
    body = dict(revision=manager.state()['revision'], consent=True, consent_version=CONSENT)
    body.update(changes)
    return manager.enable(body)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='radar-startup-')
        self.addCleanup(self.tmp.cleanup)
        self.workspace, self.executable, self.registry, self.manager = fixture(self.tmp.name)

    def test_read_is_off_and_does_not_create_metadata_or_registration(self):
        for _ in range(2):
            state = self.manager.state()
            self.assertEqual(state['status'], 'disabled'); self.assertTrue(state['can_enable'])
        self.assertFalse(self.manager.root.exists()); self.assertEqual(self.registry.writes, [])

    def test_enable_restart_disable_keep_daily_plan_and_other_entries(self):
        plan = self.workspace.root / 'unrelated-plan.json'; plan.write_bytes(b'unchanged plan')
        self.registry.values['unrelated'] = (1, 'another app')
        state = enable(self.manager)
        self.assertEqual(state['status'], 'registered'); self.assertFalse(state['can_enable'])
        other = WindowsStartup(self.workspace, executable=self.executable, registry=self.registry, portable=True, platform='win32')
        self.assertEqual(other.state(), state)
        result = other.disable({'revision':state['revision']})
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(self.registry.values, {'unrelated':(1, 'another app')})
        self.assertEqual(plan.read_bytes(), b'unchanged plan')
        self.assertTrue(self.manager.receipt_path.exists())

    def test_current_revision_and_explicit_consent_and_exact_fields_required(self):
        for change in ({'consent':False}, {'consent':1}, {'consent_version':'old'},
                       {'executable':'arbitrary'}, {'revision':'x'}, {'revision':None}):
            with self.subTest(change=change), self.assertRaises(InputError):
                enable(self.manager, **change)
        self.assertEqual(self.registry.writes, []); self.assertFalse(self.manager.root.exists())
        previous = self.manager.state()['revision']; state = enable(self.manager)
        with self.assertRaises(StartupChanged): self.manager.disable({'revision':previous})
        self.assertEqual(self.manager.state(), state)

    def test_source_and_other_platform_never_touch_registry(self):
        for platform, portable in (('win32',False), ('linux',True), ('darwin',False)):
            manager = WindowsStartup(self.workspace, registry=self.registry, portable=portable, platform=platform)
            with patch.object(self.registry, 'read', side_effect=AssertionError('registry access')):
                self.assertEqual(manager.state()['status'], 'unsupported')
                with self.assertRaises(InputError): enable(manager, revision='a'*64)
        self.assertFalse(self.manager.root.exists())

    def test_registration_write_failure_keeps_recoverable_receipt_without_success(self):
        self.registry.fail_create = True
        with self.assertRaises(InputError) as raised: enable(self.manager)
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertTrue(self.manager.receipt_path.exists()); self.assertEqual(self.manager.state()['status'], 'disabled')
        self.registry.fail_create = False
        self.assertTrue(enable(self.manager)['registered'])

    def test_receipt_failure_prevents_os_mutation_and_crash_after_write_is_owned(self):
        with patch('vibe_job_radar.windows_startup.atomic_json', side_effect=OSError('disk')):
            with self.assertRaises(OSError): enable(self.manager)
        self.assertEqual(self.registry.writes, [])
        original = self.registry.create
        def crash(name, command):
            original(name, command); raise OSError('after OS write')
        with patch.object(self.registry, 'create', side_effect=crash):
            with self.assertRaises(InputError): enable(self.manager)
        self.assertEqual(self.manager.state()['status'], 'registered')
        self.manager.disable({'revision':self.manager.state()['revision']})
        self.assertEqual(self.registry.values, {})

    def test_unknown_or_modified_values_are_never_overwritten_or_deleted(self):
        self.registry.values[self.manager.name] = (1, 'PRIVATE FOREIGN COMMAND')
        state = self.manager.state(); self.assertEqual(state['status'], 'conflict')
        self.assertNotIn('PRIVATE', json.dumps(state))
        with self.assertRaises(InputError): enable(self.manager)
        with self.assertRaises(InputError): self.manager.disable({'revision':state['revision']})
        self.assertEqual(self.registry.writes, [])
        del self.registry.values[self.manager.name]; enable(self.manager)
        self.registry.values[self.manager.name] = (2, 'PRIVATE FOREIGN COMMAND')
        state = self.manager.state(); self.assertEqual(state['status'], 'conflict')
        with self.assertRaises(InputError): self.manager.disable({'revision':state['revision']})
        self.assertEqual(len(self.registry.writes), 1)

    def test_moved_package_requires_disable_then_fresh_consent(self):
        enable(self.manager)
        new_exe = self.executable.parent.parent / 'new' / self.executable.name
        new_exe.parent.mkdir(); new_exe.write_bytes(b'new fixture'); self.executable.unlink()
        other = WindowsStartup(self.workspace, executable=new_exe, registry=self.registry, portable=True, platform='win32')
        state = other.state(); self.assertEqual(state['status'], 'moved'); self.assertTrue(state['can_disable'])
        with self.assertRaises(InputError): enable(other)
        other.disable({'revision':state['revision']})
        self.assertEqual(enable(other)['status'], 'registered')
        self.assertIn(str(new_exe), self.registry.values[other.name][1])

    def test_remove_failure_does_not_claim_disabled(self):
        state = enable(self.manager); self.registry.fail_remove = True
        with self.assertRaises(InputError) as raised: self.manager.disable({'revision':state['revision']})
        self.assertNotIn('PRIVATE', str(raised.exception)); self.assertTrue(self.manager.state()['registered'])

    def test_corrupt_or_cross_workspace_receipt_leaves_registration_untouched(self):
        state = enable(self.manager); original = self.manager.receipt_path.read_text(encoding='utf-8')
        for invalid in ('{', '{"schema_version":1,"schema_version":1}', 'x'*8193,
                        json.dumps({**json.loads(original),'workspace':'different'})):
            self.manager.receipt_path.write_text(invalid, encoding='utf-8')
            self.assertEqual(self.manager.state()['status'], 'unavailable')
            with self.assertRaises(InputError): self.manager.disable({'revision':state['revision']})
        self.assertEqual(len(self.registry.writes), 1)

    def test_symlink_receipt_is_not_read_or_followed(self):
        self.manager.root.mkdir(); target = self.workspace.root / 'unrelated.json'; target.write_text('{}', encoding='utf-8')
        try: self.manager.receipt_path.symlink_to(target)
        except OSError: self.skipTest('symlink not available')
        self.assertEqual(self.manager.state()['status'], 'unavailable')
        with self.assertRaises(InputError): enable(self.manager, revision='a'*64)
        self.assertEqual(target.read_text(encoding='utf-8'), '{}'); self.assertEqual(self.registry.writes, [])

    def test_registry_read_error_is_redacted_and_never_becomes_disabled(self):
        with patch.object(self.registry, 'read', side_effect=PermissionError('PRIVATE COMMAND')):
            state = self.manager.state()
            self.assertEqual(state['status'], 'unavailable'); self.assertFalse(state['can_enable'])
            self.assertNotIn('PRIVATE', json.dumps(state))

    def test_distinct_workspaces_have_distinct_registration_names(self):
        other = WindowsStartup(Workspace(Path(self.tmp.name)/'another'), executable=self.executable,
                               registry=self.registry, portable=True, platform='win32')
        self.assertNotEqual(other.name, self.manager.name)
        enable(other); enable(self.manager)
        self.manager.disable({'revision':self.manager.state()['revision']})
        self.assertEqual(list(self.registry.values), [other.name])

    def test_command_rejects_long_and_ambiguous_paths(self):
        for value in ('has\nnewline', '"quoted"', '%PROGRAMDATA%\\app', 'x'*260, '\0'):
            with self.subTest(value=value), self.assertRaises(InputError):
                command_line('C:\\VibeJobRadar.exe', value)
        self.assertEqual(command_line(r'C:\雷达 & app\VibeJobRadar.exe', 'D:\\'),
                         '"C:\\雷达 & app\\VibeJobRadar.exe" --workspace "D:\\\\"')
        if os.name == 'nt':
            self._assert_windows_arguments()

    def test_worker_mode_has_versioned_receipt_and_same_owned_value_across_restart(self):
        state = enable(self.manager, mode='public_worker', consent_version=MODE_CONSENT)
        self.assertEqual((state['status'], state['mode']), ('registered', 'public_worker'))
        receipt = json.loads(self.manager.receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(receipt['schema_version'], 2)
        self.assertEqual(receipt['command'], command_line(self.executable, self.workspace.root, 'public_worker'))
        other = WindowsStartup(self.workspace, executable=self.executable, registry=self.registry, portable=True, platform='win32')
        self.assertEqual(other.state(), state)
        before = self.manager.receipt_path.read_bytes()
        with self.assertRaises(InputError):enable(other)
        self.assertEqual(self.manager.receipt_path.read_bytes(), before)
        disabled = other.disable({'revision':state['revision']})
        self.assertEqual((disabled['status'], disabled['mode']), ('disabled', 'workbench'))
        self.assertFalse((self.workspace.root/'public_schedule').exists())
        # Fresh legacy consent after disabling remains the old v1 workbench.
        self.assertEqual(enable(other)['mode'], 'workbench')
        receipt = json.loads(self.manager.receipt_path.read_text(encoding='utf-8'))
        self.assertEqual(receipt['schema_version'], 1);self.assertNotIn('mode', receipt)

    def test_worker_requires_new_consent_and_exact_fixed_mode(self):
        for change in ({'mode':'public_worker'}, {'consent_version':MODE_CONSENT},
                       {'mode':'workbench'}, {'mode':None}, {'mode':[]},
                       {'mode':'public_worker --other'}, {'mode':'worker'},
                       {'mode':'public_worker', 'command':'arbitrary'}):
            with self.subTest(fields=tuple(change)),self.assertRaises(InputError):
                enable(self.manager, **change)
        for mode in (None, [], '--public-worker', 'unknown'):
            with self.assertRaises(InputError):command_line(self.executable, self.workspace.root, mode)
        self.assertFalse(self.manager.root.exists());self.assertEqual(self.registry.writes, [])
        state = enable(self.manager, mode='workbench', consent_version=MODE_CONSENT)
        self.assertEqual(state['mode'], 'workbench')
        self.assertNotIn('--public-worker', self.registry.values[self.manager.name][1])

    def test_worker_receipt_mode_or_version_tampering_cannot_remove_registration(self):
        state = enable(self.manager, mode='public_worker', consent_version=MODE_CONSENT)
        original = json.loads(self.manager.receipt_path.read_text(encoding='utf-8'))
        for changes in ({'schema_version':1}, {'mode':'workbench'}, {'mode':None},
                        {'command':command_line(self.executable,self.workspace.root)},
                        {'schema_version':3}):
            self.manager.receipt_path.write_text(json.dumps({**original,**changes}), encoding='utf-8')
            self.assertEqual(self.manager.state()['status'], 'unavailable')
            with self.assertRaises(InputError):self.manager.disable({'revision':state['revision']})
        self.assertEqual(len(self.registry.writes), 1)

    def test_moved_worker_and_failed_registration_remain_recoverable(self):
        self.registry.fail_create = True
        with self.assertRaises(InputError):enable(self.manager, mode='public_worker', consent_version=MODE_CONSENT)
        self.assertEqual(self.manager.state()['status'], 'disabled')
        self.registry.fail_create = False
        enable(self.manager, mode='public_worker', consent_version=MODE_CONSENT)
        new_exe = self.executable.parent.parent/'new'/self.executable.name
        new_exe.parent.mkdir();new_exe.write_bytes(b'new fixture only')
        other = WindowsStartup(self.workspace, executable=new_exe, registry=self.registry, portable=True, platform='win32')
        state = other.state();self.assertEqual((state['status'], state['mode']), ('moved', 'public_worker'))
        other.disable({'revision':state['revision']})
        self.assertEqual(enable(other, mode='public_worker', consent_version=MODE_CONSENT)['status'], 'registered')

    def _assert_windows_arguments(self):
        from ctypes import wintypes
        shell = ctypes.WinDLL('shell32'); kernel = ctypes.WinDLL('kernel32')
        shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        kernel.LocalFree.argtypes = [wintypes.HLOCAL]; kernel.LocalFree.restype = wintypes.HLOCAL
        for workspace in ('D:\\', r'D:\工作区 & spaces'):
            exe = r'C:\雷达 & app\VibeJobRadar.exe'; count = ctypes.c_int()
            for mode in ('workbench','public_worker'):
                argv = shell.CommandLineToArgvW(command_line(exe, workspace, mode), ctypes.byref(count))
                expected = [exe] + (['--public-worker'] if mode=='public_worker' else []) + ['--workspace',workspace]
                try: self.assertEqual([argv[i] for i in range(count.value)], expected)
                finally: kernel.LocalFree(argv)


class StartupHTTPTests(unittest.TestCase):
    def test_auth_contract_conflict_and_original_schedule(self):
        with tempfile.TemporaryDirectory(prefix='radar-startup-http-') as temp:
            workspace, _, registry, manager = fixture(temp)
            server = LocalServer(workspace); server.windows_startup = manager
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True); thread.start()
            def call(method, action, body=None, authorized=True):
                conn = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    headers = {'Content-Type':'application/json'}
                    if authorized: headers['X-Radar-Token'] = server.token
                    conn.request(method, '/api/windows/startup/'+action,
                                 body=None if body is None else json.dumps(body), headers=headers)
                    response = conn.getresponse(); return response.status, json.loads(response.read())
                finally: conn.close()
            try:
                self.assertEqual(call('GET','state',authorized=False)[0], 403)
                _, state = call('GET','state'); self.assertEqual(registry.writes, [])
                body = dict(revision=state['revision'], consent=True, consent_version=CONSENT)
                self.assertEqual(call('POST','enable',body,False)[0], 403)
                self.assertEqual(call('POST','enable',{**body,'executable':'arbitrary'})[0], 400)
                status, changed = call('POST','enable',body); self.assertEqual(status, 200)
                self.assertEqual(call('POST','disable',{'revision':state['revision']})[0], 409)
                self.assertEqual(call('POST','disable',{'revision':changed['revision']})[0], 200)
                _, state = call('GET','state')
                worker = dict(revision=state['revision'],consent=True,consent_version=MODE_CONSENT,mode='public_worker')
                self.assertEqual(call('POST','enable',worker,False)[0], 403)
                self.assertEqual(call('POST','enable',{**worker,'consent_version':CONSENT})[0], 400)
                status, changed = call('POST','enable',worker)
                self.assertEqual(status, 200);self.assertEqual(changed['mode'],'public_worker')
                self.assertEqual(call('POST','disable',{'revision':changed['revision']})[0], 200)
                self.assertEqual(server.public_schedule.state()['status'], 'disabled')
                self.assertEqual(len(registry.writes), 4)
            finally: server.shutdown(); server.server_close(); thread.join(5)

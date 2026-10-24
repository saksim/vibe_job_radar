"""Shared services keep local behavior without owning a web listener."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.application import WorkbenchApplication
from vibe_job_radar.application_http import WorkbenchHTTP, decode_request_body
from vibe_job_radar.workbench import LocalServer
from vibe_job_radar.workspace import InputError, Workspace


CLOSE_NAMES = ('public_queue', 'collection_runner', 'public_schedule', 'team',
               'releases', 'workspace', 'guided', 'public_tasks')


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.application = WorkbenchApplication(Workspace(self.temporary.name), public_client=None)
        self.addCleanup(self.application.close)

    def watch_closes(self):
        watches = {}
        for name in CLOSE_NAMES:
            service = getattr(self.application, name)
            method = 'close_network' if name == 'workspace' else 'close'
            watcher = patch.object(service, method, wraps=getattr(service, method))
            watches[name] = watcher.start()
            self.addCleanup(watcher.stop)
        return watches

    def test_application_serves_existing_report_flow_without_a_socket(self):
        from test_workbench import capture
        with patch('socket.socket', side_effect=AssertionError('application opened a socket')):
            routes = WorkbenchHTTP(self.application)
            self.assertEqual(routes.post('/api/job', capture()).status, 200)
            response = routes.post('/api/analyze', {'roles': ['time_series']})
            self.assertEqual(response.status, 200, response.body)
            report = json.loads(response.body)
            loaded = routes.get('/api/report/' + report['id'])
            self.assertEqual(json.loads(loaded.body)['manifest'], report['manifest'])
            download = routes.get('/api/download/' + report['id'] + '/requirements_zh.csv')
            self.assertEqual(download.status, 200)
            self.assertEqual(download.filename, 'requirements_zh.csv')
            self.assertTrue(download.body)
            rejected = routes.get('/api/download/' + report['id'] + '/../jobs.sqlite')
            self.assertEqual(rejected.status, 400)

    def test_start_is_idempotent_and_shutdown_closes_each_service_once(self):
        watches = self.watch_closes()
        with patch.object(self.application.public_schedule, 'start', wraps=self.application.public_schedule.start) as schedule, \
             patch.object(self.application.public_queue, 'start', wraps=self.application.public_queue.start) as queue:
            self.application.start()
            self.application.start()
            schedule.assert_called_once_with()
            queue.assert_called_once_with()
            self.application.close()
            self.application.close()
        for watcher in watches.values():
            watcher.assert_called_once_with()
        with self.assertRaises(InputError):
            self.application.start()

    def test_successful_upgrade_then_shutdown_does_not_close_dispatchers_twice(self):
        watches = self.watch_closes()
        self.application.start()
        self.application.upgrade_quiesce()
        self.assertTrue(self.application.mutation_lock.locked())
        self.assertFalse(self.application.public_queue.is_running())
        self.assertFalse(self.application.public_schedule.is_running())
        self.application.close()
        for watcher in watches.values():
            watcher.assert_called_once_with()
        self.application.mutation_lock.release()

    def test_failed_upgrade_restarts_dispatchers_and_final_shutdown_still_closes_them(self):
        watches = self.watch_closes()
        self.application.start()
        with patch.object(self.application, 'upgrade_preflight', side_effect=[None, InputError('busy')]):
            with self.assertRaisesRegex(InputError, 'busy'):
                self.application.upgrade_quiesce()
        self.assertFalse(self.application.mutation_lock.locked())
        self.assertTrue(self.application.public_queue.is_running())
        self.assertTrue(self.application.public_schedule.is_running())
        self.application.close()
        self.assertEqual(watches['public_queue'].call_count, 2)
        self.assertEqual(watches['public_schedule'].call_count, 2)
        for name in set(CLOSE_NAMES) - {'public_queue', 'public_schedule'}:
            watches[name].assert_called_once_with()

    def test_close_failure_does_not_skip_other_service_owners_or_repeat_close(self):
        watches = self.watch_closes()
        watches['team'].side_effect = OSError('team close failed')
        with self.assertRaisesRegex(OSError, 'team close failed'):
            self.application.close()
        self.application.close()
        for watcher in watches.values():
            watcher.assert_called_once_with()

    def test_local_service_replacement_is_visible_to_the_shared_dispatcher(self):
        server = LocalServer(self.application.workspace, public_client=None)
        try:
            original = server.windows_startup
            replacement = SimpleNamespace(state=lambda: {'replacement': True})
            with patch.object(server, 'windows_startup', replacement):
                self.assertIs(server.application.windows_startup, replacement)
                self.assertEqual(json.loads(server.http.get('/api/windows/startup/state').body), {'replacement': True})
            self.assertIs(server.windows_startup, original)
            self.assertIs(server.application.windows_startup, original)
        finally:
            server.server_close()


class ApplicationHTTPTests(unittest.TestCase):
    def test_dispatch_returns_after_lock_release_and_redacts_unexpected_errors(self):
        application = SimpleNamespace(workspace=SimpleNamespace(plan=Mock(return_value={'ok': True})),
                                      mutation_lock=threading.Lock())
        routes = WorkbenchHTTP(application)
        application.mutation_lock.acquire()
        self.assertEqual(routes.post('/api/plan', {}).status, 409)
        application.workspace.plan.assert_not_called()
        application.mutation_lock.release()
        self.assertEqual(routes.post('/api/plan', {}).status, 200)
        self.assertFalse(application.mutation_lock.locked())
        application.workspace.plan.side_effect = ValueError('private document body')
        response = routes.post('/api/plan', {})
        self.assertEqual(response.status, 400)
        self.assertNotIn(b'private document body', response.body)
        self.assertFalse(application.mutation_lock.locked())
        self.assertEqual(routes.post('/api/missing', {}).status, 404)

    def test_body_decoder_keeps_strict_routes_and_object_only_contract(self):
        for path in ('/api/quality/save', '/api/team/check', '/api/releases/check', '/api/network/proxy-auth'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                decode_request_body(path, b'{"value":1,"value":2}')
        self.assertEqual(decode_request_body('/api/plan', b'{"value":1,"value":2}'), {'value': 2})
        with self.assertRaises(ValueError):
            decode_request_body('/api/plan', b'[]')


if __name__ == '__main__':
    unittest.main()
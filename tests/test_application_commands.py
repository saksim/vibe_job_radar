"""Use cases work without HTTP and keep mutation/observation ownership explicit."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.application import WorkbenchApplication
from vibe_job_radar.application_commands import ApplicationCommands, OperationBusy, UnknownCommand
from vibe_job_radar.application_http import WorkbenchHTTP
from vibe_job_radar.workspace import InputError, Workspace


class ApplicationCommandTests(unittest.TestCase):
    def test_real_report_use_case_without_a_page_socket_or_http_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = WorkbenchApplication(Workspace(temporary), public_client=None)
            try:
                with patch('socket.socket', side_effect=AssertionError('unexpected network listener')):
                    from test_workbench import capture
                    app.execute('workspace.add_job', capture())
                    result = app.execute('workspace.analyze', {'roles':['time_series']})
                    self.assertEqual(app.workspace.report(result['id'])['manifest'], result['manifest'])
                    self.assertTrue(app.workspace.report_file(result['id'],'requirements_zh.csv').is_file())
                self.assertFalse(app.mutation_lock.locked())
            finally:
                app.close()

    def test_observation_and_pause_stay_available_while_mutations_are_reserved(self):
        app = SimpleNamespace(mutation_lock=threading.Lock(),
            workspace=SimpleNamespace(analyze=Mock()),
            evidence=SimpleNamespace(state=Mock(return_value={'revision':4})),
            collection_runner=SimpleNamespace(state=Mock(return_value={'active':True}),
                pause=Mock(return_value={'status':'pausing'})),
            report_tasks=SimpleNamespace(state=Mock(return_value={'task':{'status':'running'},'busy':True})))
        commands = ApplicationCommands(app)
        routes = WorkbenchHTTP(app)
        app.mutation_lock.acquire()
        try:
            self.assertEqual(commands.execute('evidence.state',{})['revision'],4)
            self.assertEqual(routes.post('/api/collection/background/state',{}).status,200)
            self.assertEqual(routes.post('/api/collection/background/pause',{'id':'a'*32}).status,200)
            observed = routes.get('/api/reports/state')
            self.assertEqual(observed.status,200)
            self.assertTrue(json.loads(observed.body)['busy'])
            with self.assertRaises(OperationBusy): commands.execute('workspace.analyze',{})
            self.assertEqual(routes.post('/api/analyze',{}).status,409)
            app.workspace.analyze.assert_not_called()
            self.assertTrue(app.mutation_lock.locked())
        finally:
            app.mutation_lock.release()

    def test_named_boundary_rejects_private_attributes_and_releases_failed_write(self):
        plan = Mock(side_effect=ValueError('private source document'))
        app = SimpleNamespace(mutation_lock=threading.Lock(),workspace=SimpleNamespace(plan=plan))
        commands = ApplicationCommands(app)
        for name in ('workspace.__dict__','workspace.close_network','__class__',None):
            with self.subTest(name=name),self.assertRaises(UnknownCommand): commands.execute(name,{})
        with self.assertRaises(ValueError): commands.execute('workspace.plan',{})
        self.assertFalse(app.mutation_lock.locked())
        response = WorkbenchHTTP(app).post('/api/plan',{})
        self.assertEqual(response.status,400)
        self.assertNotIn(b'private source document',response.body)
        self.assertFalse(app.mutation_lock.locked())

    def test_closed_application_does_not_accept_another_named_mutation(self):
        action = Mock()
        app = SimpleNamespace(_closed=True,mutation_lock=threading.Lock(),workspace=SimpleNamespace(plan=action))
        with self.assertRaises(InputError): ApplicationCommands(app).execute('workspace.plan',{})
        action.assert_not_called()
        self.assertFalse(app.mutation_lock.locked())


if __name__ == '__main__':
    unittest.main()

"""Preserve failed native task facts before requiring success-only UI evidence."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    import run_native_liepin_search as fixture


class VisibleFormFailureTests(unittest.TestCase):
    def verify(self, task, backend, expected, *, check_record=True):
        server = SimpleNamespace(requests=[], search_shapes=[], form_mode='')
        service = Mock()
        service._backends = {'task': backend}
        ledger = Mock()
        ledger.summary.return_value = {'login': {'day': 0}}
        result = {'checks': []}
        with patch.object(fixture, 'Workspace'), patch.object(fixture, 'RateLedger', return_value=ledger), \
                patch.object(fixture, 'Registry'), patch.object(fixture, 'GuidedService', return_value=service):
            with self.assertRaises(AssertionError) as caught:
                fixture.verify_visible_forms(Path('unused-artificial-workspace'), server, Mock(), Mock(),
                                             lambda _: task, {'keyword': 'artificial'}, result)
        self.assertEqual(caught.exception.args, (expected,))
        if check_record:
            self.assertEqual(result['visible_form_last_task'],
                             {'case': 'matched', 'status': task['status'], 'code': task['code']})
        self.assertEqual(result['visible_form_results'], [])
        self.assertEqual(result['checks'], [])
        service.close.assert_called_once_with()
        self.assertEqual(server.form_mode, '')

    def test_failed_task_retains_network_stop_before_success_only_attribute(self):
        self.verify({'id': 'task', 'status': 'failed', 'code': 'local_proxy_connection_failed'},
                    SimpleNamespace(), 'local_proxy_connection_failed')

    def test_ready_task_still_requires_actual_form_observation(self):
        self.verify({'id': 'task', 'status': 'ready', 'code': 'ready'},
                    SimpleNamespace(), 'visible_form_observation_missing')

    def test_ready_observation_must_still_prove_exactly_one_submission(self):
        observed = {'submissions': 2, 'initializations': 1}
        self.verify({'id': 'task', 'status': 'ready', 'code': 'ready'},
                    SimpleNamespace(fixture_form_observation=observed), observed, check_record=False)


if __name__ == '__main__':
    unittest.main()

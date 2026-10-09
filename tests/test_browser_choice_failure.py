"""Real exception objects and the actual choice harness; no installed browser."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1]/'scripts/run_browser_choice_acceptance.py'
spec = importlib.util.spec_from_file_location('choice_failure_under_test', SOURCE)
choice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(choice)
SECRET = 'fixture-secret-token-user-profile-must-not-be-retained'


def service():
    return SimpleNamespace(_lock=threading.Lock(), _busy=True,
        _setup={'stage':'launch_check','message':SECRET},
        _selected_browser='bundled',
        _browser_health={'ready':False,'stage':'launch','code':'browser_launch_timeout',
                         'selection_applied':False,'browser_channel':'msedge',
                         'network_backend':'bridge','launch_tested':False,
                         'error_summary':SECRET,'executable_path':SECRET})


def original_error():
    try:
        raise AssertionError(SECRET)
    except AssertionError as error:
        return error


class ChoiceFailureEvidenceTests(unittest.TestCase):
    def test_health_and_choice_facts_are_saved_without_secret_data_or_service_read(self):
        state = service()
        state.state = Mock(side_effect=AssertionError('full service read is forbidden'))
        result = {'checkpoint':'selected-choice-visible'}
        choice.record_choice_failure(result, state, original_error())
        row = result['first_failure']
        self.assertTrue(row['service_facts']['available'])
        self.assertEqual(row['service_facts']['selected_browser'], 'bundled')
        self.assertEqual(row['service_facts']['setup_stage'], 'launch_check')
        self.assertEqual(row['service_facts']['health']['code'], 'browser_launch_timeout')
        self.assertFalse(row['service_facts']['health']['ready'])
        self.assertEqual(row['checkpoint'], 'selected-choice-visible')
        self.assertNotIn(SECRET, json.dumps(row))
        state.state.assert_not_called()

    def test_busy_lock_is_unavailable_without_waiting_or_false_success(self):
        state = service()
        state._lock.acquire()
        try:
            result = {'checkpoint':'selected-choice-visible'}
            choice.record_choice_failure(result, state, original_error())
            self.assertEqual(result['first_failure']['service_facts'], {'available':False})
        finally:
            state._lock.release()

    def test_unknown_health_values_and_exception_sites_are_not_echoed(self):
        state = service()
        state._selected_browser = SECRET
        state._setup['stage'] = SECRET
        state._browser_health.update(ready=1, code=SECRET, stage=SECRET, browser_channel=SECRET)
        result = {'checkpoint':SECRET}
        choice.record_choice_failure(result, state, original_error())
        row = result['first_failure']
        self.assertEqual(row['checkpoint'], 'unknown')
        self.assertIsNone(row['assertion_site'])
        self.assertIsNone(row['service_facts']['health']['ready'])
        self.assertEqual(row['service_facts']['health']['code'], 'unknown')
        self.assertEqual(row['service_facts']['selected_browser'], 'unknown')
        self.assertNotIn(SECRET, json.dumps(row))

    def test_first_snapshot_is_independent_and_cannot_be_overwritten_by_cleanup(self):
        state = service()
        result = {'checkpoint':'selected-choice-visible'}
        choice.record_choice_failure(result, state, original_error())
        original = json.dumps(result['first_failure'], sort_keys=True)
        state._setup['stage'] = 'ready'
        state._browser_health.update(ready=True, code='browser_ready')
        result['checkpoint'] = 'native-choice-check'
        choice.record_choice_failure(result, state, RuntimeError(SECRET))
        self.assertEqual(json.dumps(result['first_failure'], sort_keys=True), original)

    def test_observer_fault_does_not_raise_or_replace_original_failure(self):
        state = service()
        state._lock = None
        result = {'checkpoint':'selected-choice-visible'}
        failure = original_error()
        choice.record_choice_failure(result, state, failure)
        self.assertTrue(result['first_failure']['observer_failed'])
        self.assertEqual(result['first_failure']['service_facts'], {'available':False})
        self.assertNotIn(SECRET, json.dumps(result))

    def test_actual_main_records_choice_assertion_before_browser_and_server_close(self):
        marks = []
        state = service()
        page = Mock()
        page.url = 'http://127.0.0.1:12345/'
        page.goto.return_value.status = 200
        page.locator.side_effect = lambda selector: SimpleNamespace(
            selector=selector, click=lambda:None, select_option=lambda value:None)
        def expect(locator):
            def contain(value, **kwargs):
                if locator.selector == '#browser-selected':
                    marks.append('original-assertion')
                    raise AssertionError(SECRET)
            return SimpleNamespace(to_contain_text=contain, to_be_visible=lambda:None)
        browser = Mock()
        browser.new_context.return_value.new_page.return_value = page
        browser.close.side_effect = lambda:marks.append('browser-close')
        pw = Mock()
        pw.chromium.launch.return_value = browser
        api = Mock()
        api.__enter__ = Mock(return_value=pw)
        api.__exit__ = Mock(return_value=False)
        fake_module = SimpleNamespace(sync_playwright=lambda:api, expect=expect)
        server = SimpleNamespace(guided=state, origin=page.url[:-1],
            entry_url=page.url+'#token='+SECRET, serve_forever=lambda **kwargs:None,
            shutdown=lambda:marks.append('server-shutdown'), server_close=lambda:None)
        recorder = getattr(choice, 'record_choice_failure', None)
        def record(*args):
            marks.append('failure-snapshot')
            return recorder(*args)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(choice,'ROOT',root), patch.object(choice,'LocalServer',return_value=server), \
                    patch.object(choice.sys,'argv',['choice','--channel','msedge']), \
                    patch.dict(sys.modules,{'playwright.sync_api':fake_module}), \
                    patch.object(choice,'record_choice_failure',side_effect=record,create=True):
                with self.assertRaisesRegex(RuntimeError, 'browser choice acceptance failed'):
                    choice.main()
            row = json.loads((root/'browser-acceptance/browser-choice/results.json').read_text(encoding='utf8'))
        self.assertEqual(marks, ['original-assertion','failure-snapshot','browser-close','server-shutdown'])
        self.assertFalse(row['success'])
        self.assertEqual(row['first_failure']['checkpoint'], 'selected-choice-visible')
        self.assertTrue(row['failure_observer_installed'])
        site = row['first_failure']['assertion_site']
        self.assertEqual(site['function'], 'main')
        self.assertEqual(site['script'], 'run_browser_choice_acceptance.py')
        self.assertIn('browser-selected', SOURCE.read_text(encoding='utf8').splitlines()[site['line']-1])
        self.assertNotIn(SECRET, json.dumps(row))
        self.assertEqual(len(row['checks']), 3)


if __name__ == '__main__':
    unittest.main()

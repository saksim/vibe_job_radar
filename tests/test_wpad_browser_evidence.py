"""Failure evidence uses safe finite fields while keeping the UI gate failed."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/run_wpad_browser.py'
spec = importlib.util.spec_from_file_location('wpad_browser_under_test', SOURCE)
wpad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wpad)
ORIGIN = 'http://127.0.0.1:43210'
SECRET = 'private-token-and-workspace-must-not-be-recorded'


def result():
    return {'page_errors':[], 'page_error_details':[],
            'page_error_details_truncated':False, 'page_error_observer_failed':False}


class WpadBrowserEvidenceTests(unittest.TestCase):
    def test_same_origin_source_locations_survive_without_private_error_text(self):
        value = result()
        error = SimpleNamespace(name='TypeError', message='Failed to fetch ' + SECRET,
            stack=f"""TypeError: {SECRET}
    at fetchState ({ORIGIN}/network-settings.js?token={SECRET}:124:17)
    at {ORIGIN}/guided.js:163:23
    at https://outside.invalid/app.js:9:10
    at {ORIGIN}/private/{SECRET}.js:9:10
    at http://user:{SECRET}@127.0.0.1:43210/app.js:9:10
    at {ORIGIN}/app.js:0:2""")
        wpad.record_page_error(value, error, ORIGIN, 'open-guided', 'main')
        self.assertEqual(value['page_errors'], ['Error'])
        self.assertEqual(value['page_error_details'], [{
            'phase':'open-guided', 'page':'main', 'name':'TypeError',
            'message_code':'fetch_failed', 'frames':[
                {'script':'network-settings.js', 'line':124, 'column':17},
                {'script':'guided.js', 'line':163, 'column':23}]}])
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertNotIn(ORIGIN, json.dumps(value))

    def test_repeated_and_deep_errors_are_bounded_without_losing_first_failure(self):
        value = result()
        error = SimpleNamespace(name='Error', message=SECRET,
            stack='\n'.join(f'at {ORIGIN}/app.js:{line}:2' for line in range(1, 25)))
        wpad.record_page_error(value, error, ORIGIN, 'confirm-stale', 'stale')
        first = json.dumps(value['page_error_details'][0], sort_keys=True)
        for _ in range(32):
            wpad.record_page_error(value, error, ORIGIN, 'rollback', 'main')
        self.assertEqual(len(value['page_errors']), 16)
        self.assertEqual(len(value['page_error_details']), 16)
        self.assertTrue(value['page_error_details_truncated'])
        self.assertEqual(len(value['page_error_details'][0]['frames']), 8)
        self.assertEqual(first, json.dumps(value['page_error_details'][0], sort_keys=True))
        self.assertNotIn(SECRET, json.dumps(value))

    def test_unknown_name_phase_page_and_foreign_stack_are_never_echoed(self):
        value = result()
        error = SimpleNamespace(name=SECRET, message=SECRET,
            stack=f'at file:///C:/Users/{SECRET}/app.js:10:2\nat https://{SECRET}/app.js:10:2')
        wpad.record_page_error(value, error, ORIGIN, SECRET, SECRET)
        self.assertEqual(value['page_error_details'][0], {
            'phase':'unknown','page':'unknown','name':'unknown','message_code':'unknown','frames':[]})
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertFalse(value['page_error_observer_failed'])
        self.assertTrue(value['page_errors'])

    def test_exception_inspection_failure_does_not_raise_or_report_success(self):
        class BrokenError:
            @property
            def name(self):
                raise RuntimeError(SECRET)
        value = result()
        wpad.record_page_error(value, BrokenError(), ORIGIN, 'check-script', 'main')
        self.assertTrue(value['page_error_observer_failed'])
        self.assertEqual(value['page_errors'], ['Error'])
        self.assertEqual(value['page_error_details'][0]['phase'], 'check-script')
        self.assertNotIn(SECRET, json.dumps(value))


if __name__ == '__main__':
    unittest.main()

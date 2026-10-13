"""Artificial tracebacks verify useful locations without private exception data."""
import json
from pathlib import Path
import tempfile
import unittest

from portable_failure_diagnostics import SCRIPTS, BrowserUiObservation, failure_frames


class PortableFailureDiagnosticsTests(unittest.TestCase):
    def trace(self, code, filename):
        try:
            exec(compile(code, str(filename), 'exec'), {})
        except AssertionError as error:
            return failure_frames(error)
        self.fail('artificial exception was not raised')

    def test_only_allowed_code_locations_survive_without_messages_paths_or_locals(self):
        frames=self.trace("def artificial():\n private='PRIVATE_TOKEN_AND_SCRIPT'\n raise AssertionError(private)\nartificial()\n",
                          SCRIPTS/'system_pac_acceptance.py')
        self.assertEqual(frames,[{'file':'system_pac_acceptance.py','function':'<module>','line':4},
                                 {'file':'system_pac_acceptance.py','function':'artificial','line':3}])
        value=json.dumps(frames)
        self.assertNotIn('PRIVATE',value);self.assertNotIn(str(SCRIPTS),value)
        self.assertNotIn('AssertionError',value);self.assertNotIn('private',value)

    def test_other_modules_and_same_basename_elsewhere_are_omitted_and_depth_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.trace("raise AssertionError('PRIVATE')",Path(tmp)/'system_pac_acceptance.py'),[])
        self.assertEqual(self.trace("raise AssertionError('PRIVATE')",SCRIPTS/'unapproved.py'),[])
        frames=self.trace("def recurse(n):\n if n: return recurse(n-1)\n raise AssertionError('PRIVATE')\nrecurse(20)\n",
                          SCRIPTS/'verify_windows_portable.py')
        self.assertEqual(len(frames),8)
        self.assertTrue(all(set(row)=={'file','function','line'} for row in frames))
        self.assertNotIn('PRIVATE',json.dumps(frames))


class UiLifecycleTests(unittest.TestCase):
    class Surface:
        def __init__(self):
            self.handlers = {}
            self.closed = False
        def on(self, event, handler):
            self.handlers[event] = handler
        def emit(self, event):
            if event == 'close':
                self.closed = True
            if event in self.handlers:
                self.handlers[event](self)
        def is_closed(self):
            return self.closed
        def is_connected(self):
            return not self.closed

    def test_failure_observation_is_not_replaced_by_verifier_cleanup(self):
        browser, context, page = (self.Surface() for _ in range(3))
        observation = BrowserUiObservation(browser)
        observation.watch_context(context)
        observation.watch_page(page)
        page.emit('crash')
        before = observation.snapshot()
        self.assertTrue(before['events_before_cleanup']['page_crashed'])
        self.assertFalse(before['page_closed'])
        self.assertTrue(before['browser_connected'])
        page.emit('close')
        context.emit('close')
        browser.closed = True
        browser.emit('disconnected')
        self.assertFalse(before['events_before_cleanup']['page_closed'])
        self.assertFalse(before['events_before_cleanup']['context_closed'])
        self.assertFalse(before['events_before_cleanup']['browser_disconnected'])
        closed = observation.snapshot()
        self.assertTrue(all(closed['events_before_cleanup'].values()))
        self.assertTrue(closed['page_closed'])
        self.assertFalse(closed['browser_connected'])

    def test_missing_observations_stay_unknown_without_hiding_original_failure(self):
        class Unavailable:
            def on(self, *_):
                raise RuntimeError('PRIVATE_URL_OR_TOKEN')
            def is_closed(self):
                raise RuntimeError('PRIVATE_URL_OR_TOKEN')
            def is_connected(self):
                raise RuntimeError('PRIVATE_URL_OR_TOKEN')
        observation = BrowserUiObservation(Unavailable())
        observation.watch_context(Unavailable())
        observation.watch_page(Unavailable())
        result = observation.snapshot()
        self.assertTrue(result['observer_failed'])
        self.assertIsNone(result['page_closed'])
        self.assertIsNone(result['browser_connected'])
        self.assertNotIn('PRIVATE', json.dumps(result))

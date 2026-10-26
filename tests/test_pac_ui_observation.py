"""Pure PAC UI observation contracts; no browser, network or repeat acceptance."""
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT/'scripts'), str(ROOT/'src'), *sys.path]):
    import run_pac_browser as probe

SECRET = 'PRIVATE_TOKEN_BODY_URL_VALUE'


class Page:
    def __init__(self):
        self.callbacks = {}
        self.url = 'http://127.0.0.1:1234/?token=' + SECRET

    def on(self, name, callback):
        self.callbacks[name] = callback

    def locator(self, *args, **kwargs):
        raise AssertionError('new DOM call forbidden')

    def evaluate(self, *args, **kwargs):
        raise AssertionError('new DOM call forbidden')


def request(url):
    class Request:
        method = 'GET'
        @property
        def headers(self): raise AssertionError('private headers accessed')
        @property
        def post_data(self): raise AssertionError('private body accessed')
    value = Request()
    value.url = url
    return value


class PacUiObservationTests(unittest.TestCase):
    def test_fixed_lifecycle_and_bounded_failure_snapshot_preserve_original_error(self):
        page = Page()
        observer = probe.PacUiObservation(page, 'http://127.0.0.1:1234')
        selected = request('http://127.0.0.1:1234/api/network/state?private=' + SECRET)
        ignored = request('http://other.invalid/api/network/state?private=' + SECRET)
        observer.record('request', ignored)
        page.callbacks['request'](selected)
        self.assertIs(next(iter(observer.pending)), selected)
        for kind, item in (('response', SimpleNamespace(request=selected, status=503)),
                           ('requestfinished', selected)):
            page.callbacks[kind](item)
        original = AssertionError('original disabled assertion')
        result = {}
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('subprocess forbidden')):
            with self.assertRaises(AssertionError) as raised:
                with probe.observe_expand_failure(observer, result):
                    raise original
        self.assertIs(raised.exception, original)
        facts = result['expand_failure']
        self.assertEqual([row['event'] for row in facts['events']],
                         ['request', 'response', 'requestfinished'])
        self.assertEqual([row['sequence'] for row in facts['events']], [1, 1, 1])
        self.assertEqual(facts['events'][1]['status'], 503)
        self.assertEqual(facts['pending'], 0)
        self.assertEqual(facts['page'], 'home')
        self.assertFalse(facts['dom_collected'])
        self.assertEqual(facts['dom_reason'], 'original_assertion_aria_only')
        self.assertNotIn(SECRET, json.dumps(facts))
        self.assertTrue(facts['threads']['threads'])
        self.assertLessEqual(len(facts['threads']['threads']), 16)
        for thread in facts['threads']['threads']:
            self.assertLessEqual(len(thread['stack']), 24)
            for frame in thread['stack']:
                self.assertEqual(set(frame), {'file', 'line', 'function'})

    def test_callback_error_and_overflow_are_bounded_without_changing_success(self):
        page = Page()
        observer = probe.PacUiObservation(page, 'http://127.0.0.1:1234')
        selected = request('http://127.0.0.1:1234/api/network/state')
        observer.record('request', selected)
        observer.record('response', SimpleNamespace(request=selected, status=SECRET))
        self.assertFalse(observer.complete)
        self.assertIsNone(observer.events[-1]['status'])
        observer.record('requestfailed', selected)
        for _ in range(40):
            observer.record('request', selected)
            observer.record('requestfailed', selected)
        observer.record('response', SimpleNamespace(request=selected))
        self.assertEqual(len(observer.events), 64)
        self.assertTrue(observer.overflow)
        self.assertFalse(observer.complete)
        self.assertEqual(observer.errors, 1)
        for _ in range(17):
            observer.record('request', request('http://127.0.0.1:1234/api/network/state'))
        self.assertEqual(len(observer.pending), 16)
        self.assertTrue(all(not isinstance(item, int) for item in observer.pending))
        result = {}
        with patch.object(observer, 'snapshot', side_effect=AssertionError('success snapshot forbidden')):
            with probe.observe_expand_failure(observer, result):
                result['business_passed'] = True
        self.assertEqual(result, {'business_passed': True})
        self.assertNotIn(SECRET, json.dumps(observer.events))
        with patch.object(page, 'on', side_effect=ValueError(SECRET)):
            unavailable = probe.PacUiObservation(page, 'http://127.0.0.1:1234')
        self.assertFalse(unavailable.available)
        self.assertFalse(unavailable.complete)

    def test_observer_capture_failures_do_not_mask_original_or_cleanup(self):
        page = Page()
        observer = probe.PacUiObservation(page, 'http://127.0.0.1:1234')
        original = RuntimeError('original failure')
        events = []
        for stage in ('threads', 'snapshot', 'missing_observer'):
            result = {}
            current = observer
            with self.subTest(stage=stage), ExitStack() as stack:
                if stage == 'snapshot':
                    stack.enter_context(patch.object(observer, 'snapshot', side_effect=ValueError(SECRET)))
                elif stage == 'threads':
                    stack.enter_context(patch.object(probe, 'failure_threads', side_effect=ValueError(SECRET)))
                else:
                    current = None
                with self.assertRaises(RuntimeError) as raised:
                    try:
                        with probe.observe_expand_failure(current, result):
                            raise original
                    finally:
                        events.append('cleanup')
                self.assertIs(raised.exception, original)
                self.assertNotIn(SECRET, json.dumps(result))
                self.assertFalse(result['expand_failure']['complete'])
                if stage == 'threads':
                    self.assertTrue(result['expand_failure']['thread_capture_failed'])
                else:
                    self.assertTrue(result['expand_failure']['capture_failed'])
        self.assertEqual(events, ['cleanup', 'cleanup', 'cleanup'])

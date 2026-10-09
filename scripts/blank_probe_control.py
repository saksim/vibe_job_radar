"""Owned offline browser control for the SDK's set_content console dependency.

The injected missing console notification is a mechanism control, not an
attribution of any earlier CI failure. Never modify an installed SDK or profile.
"""
from __future__ import annotations

from threading import Event
from unittest.mock import patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.browser_health import _OfflineTransport, probe_browser


def suppress_console_notification(backend):
    contexts = []
    session = backend.context.new_cdp_session(backend.page)
    session.on('Runtime.executionContextCreated',
               lambda event: contexts.append(event['context']))
    session.send('Runtime.enable')
    # The SDK itself creates its utility world; the fixture observes that world.
    backend.page.title()
    utility = [item for item in contexts
               if item.get('name', '').startswith('__playwright_utility_world_')
               and item.get('auxData', {}).get('isDefault') is False]
    if len(utility) != 1:
        raise AssertionError('expected one SDK utility context')
    reply = session.send('Runtime.evaluate', {
        'expression': 'console.debug = () => undefined; true',
        'contextId': utility[0]['id'], 'returnByValue': True})
    if reply.get('exceptionDetails') or reply.get('result', {}).get('value') is not True:
        raise AssertionError('controlled console suppression failed')


def verify_inline_probe(*, executable_path=None):
    from playwright.sync_api import TimeoutError as BrowserTimeout

    result = {'success': False, 'old_operation_timed_out': False,
              'old_title_written': False, 'new_probe_ready': False,
              'non_inline_page_requests': 0, 'backends_created': 0,
              'backends_closed': 0, 'timeout_ms': 6000,
              'original_CI_root_cause_confirmed': False}
    original = PlaywrightBackend

    def create(*args, **kwargs):
        backend = original(*args, **kwargs)
        result['backends_created'] += 1
        close = backend.close
        closed = False

        def close_owned():
            nonlocal closed
            try:
                return close()
            finally:
                if not closed:
                    closed = True
                    result['backends_closed'] += 1
        backend.close = close_owned

        def requested(request):
            if not request.url.startswith(('data:', 'about:')):
                result['non_inline_page_requests'] += 1
        backend.page.on('request', requested)
        try:
            suppress_console_notification(backend)
        except BaseException:
            backend.close()
            raise
        return backend

    backend = create(builtins().get('boss'), None, Event(), headless=True,
                     executable_path=executable_path,
                     transport_factory=_OfflineTransport)
    try:
        try:
            backend.page.set_content(
                '<title>Vibe Radar browser check</title><p>本机浏览器检查成功</p>',
                wait_until='domcontentloaded', timeout=6000)
        except BrowserTimeout:
            result['old_operation_timed_out'] = True
        result['old_title_written'] = backend.page.title() == 'Vibe Radar browser check'
        assert result['old_operation_timed_out'] and result['old_title_written']
    finally:
        backend.close()

    with patch('vibe_job_radar.guided.browser.PlaywrightBackend', side_effect=create):
        health = probe_browser(headless=True, executable_path=executable_path)
    result['new_probe_ready'] = health['ready']
    result['new_step'] = health.get('blank_page_check', {}).get('step')
    assert result['new_probe_ready'] and result['new_step'] == 'verified'
    assert result['backends_created'] == result['backends_closed'] == 2
    assert result['non_inline_page_requests'] == 0
    result['success'] = True
    return result

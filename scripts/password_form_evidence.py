"""Bounded metadata for the synthetic password-form acceptance fixture only."""
from collections import deque
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

SCENARIOS = frozenset({
    'submit_once', 'unchecked_agreement', 'revoked_agreement', 'hidden_duplicate',
    'missing_agreement', 'duplicate_agreement', 'captcha', 'foreign_action',
    'cancelled', 'changed_form', 'sms_overlay', 'sms_inline', 'closed_login',
    'hidden_switch', 'delayed_switch', 'duplicate_switch', 'unchanged_sms',
})
PHASES = frozenset({'browser_start', 'page_load', 'entry_setup', 'password_submit',
                    'assertions', 'manual_submit', 'browser_version', 'cleanup'})
METHODS = frozenset({'Page.navigate', 'Fetch.enable', 'Fetch.fulfillRequest',
                     'Runtime.evaluate', 'Input.dispatchMouseEvent',
                     'Input.dispatchKeyEvent', 'Input.insertText'})
ERROR_MESSAGES = {
    'Invalid InterceptionId.': 'invalid_interception_id',
    'Invalid InterceptionId': 'invalid_interception_id',
    'Invalid parameters': 'invalid_parameters',
    'No resource with given identifier found': 'missing_resource',
    'Session with given id not found': 'missing_session',
}


class FormEvidence:
    def __init__(self, channel, native):
        if channel not in {None, 'chrome', 'msedge'}:
            raise ValueError('unsupported fixture channel')
        self.channel, self.native = channel, native
        self.phase, self.scenario = 'browser_start', None
        self.checks, self.version, self.failure = [], None, None
        self.interception_ready = False
        self.protocol_events = deque(maxlen=32)
        self.requests, self.pending = {}, {}
        self.dropped = 0

    def enter(self, phase, scenario=None):
        if phase not in PHASES or scenario is not None and scenario not in SCENARIOS:
            raise ValueError('unsupported fixture stage')
        self.phase = phase
        if scenario is not None:
            self.scenario = scenario

    def fail(self, exc):
        if self.failure is None:
            code = getattr(exc, 'code', None)
            known = {'native_protocol_error', 'native_observation_limit', 'browser_closed',
                     'page_not_ready', 'paused'}
            self.failure = {'phase': self.phase, 'scenario': self.scenario,
                            'code': code if isinstance(code, str) and code in known else 'fixture_failure',
                            'kind': type(exc).__name__ if type(exc).__name__ in {
                                'CrawlError', 'PageOperationError', 'AssertionError',
                                'KeyboardInterrupt', 'TimeoutError'} else 'other'}

    @staticmethod
    def decode(raw):
        if not isinstance(raw, str) or len(raw) > 64_000:
            return {}
        try:
            message = json.loads(raw)
        except (ValueError, RecursionError):
            return {}
        return message if isinstance(message, dict) else {}

    def remember(self, mapping, key, value, limit):
        if key not in mapping and len(mapping) >= limit:
            mapping.pop(next(iter(mapping)))
            self.dropped += 1
        mapping[key] = value

    def sent(self, raw):
        message = self.decode(raw)
        method = message.get('method')
        if not isinstance(method, str) or method not in METHODS or type(message.get('id')) is not int:
            return
        params = message.get('params')
        params = params if isinstance(params, dict) else {}
        request_id = params.get('requestId')
        request = self.requests.pop(request_id, None) if isinstance(request_id, str) else None
        self.remember(self.pending, message['id'], {
            'method': method, 'phase': self.phase, 'scenario': self.scenario,
            'request': request}, 32)

    def received(self, raw):
        message = self.decode(raw)
        params = message.get('params')
        if message.get('method') == 'Fetch.requestPaused' and isinstance(params, dict):
            request_id, request = params.get('requestId'), params.get('request')
            if isinstance(request_id, str) and len(request_id) <= 256 and isinstance(request, dict):
                resource = params.get('resourceType')
                resource = resource if isinstance(resource, str) and resource in {
                    'Document', 'Stylesheet', 'Image', 'Script', 'XHR', 'Fetch', 'Other'} else 'other'
                role = 'document' if resource == 'Document' else 'other'
                url = request.get('url')
                if role == 'other' and isinstance(url, str) and len(url) <= 4096:
                    try:
                        if urlsplit(url).path == '/favicon.ico':
                            role = 'favicon'
                    except ValueError:
                        pass
                self.remember(self.requests, request_id, {'resource_type': resource, 'role': role}, 64)
        ident = message.get('id')
        command = self.pending.pop(ident, None) if type(ident) is int else None
        error = message.get('error')
        if command is not None and isinstance(error, dict):
            code, text = error.get('code'), error.get('message')
            category = ERROR_MESSAGES.get(text, 'other_protocol_error') if isinstance(text, str) else 'other_protocol_error'
            if len(self.protocol_events) == self.protocol_events.maxlen:
                self.dropped += 1
            self.protocol_events.append({**command,
                'error_code': code if type(code) is int and -1_000_000 <= code <= 1_000_000 else None,
                'error_category': category})

    def wrap(self, transport):
        evidence = self

        class ObservedTransport:
            def send(self, raw):
                evidence.sent(raw)
                return transport.send(raw)

            def recv(self, *args, **kwargs):
                raw = transport.recv(*args, **kwargs)
                evidence.received(raw)
                return raw

            def close(self):
                return transport.close()

            def __getattr__(self, name):
                return getattr(transport, name)

        return ObservedTransport()

    def report(self, success):
        version = self.version if isinstance(self.version, str) and len(self.version) <= 64 and re.fullmatch(r'\d+(?:\.\d+){1,5}', self.version) else None
        return {'success': success, 'checks': list(self.checks), 'external_requests': 0 if success or self.interception_ready else None,
                'external_requests_scope': 'Owned synthetic fixture page; browser background requests are not counted.',
                'fixture_interception_ready': self.interception_ready,
                'real_account_tested': False, 'input_backend': 'minimal_cdp' if self.native else 'playwright',
                'browser_version': version, 'failure': self.failure,
                'protocol_errors': list(self.protocol_events), 'diagnostic_dropped': self.dropped,
                'scope': 'Synthetic local form fixture; metadata only, no raw protocol messages or credentials.'}

    def publish(self, success):
        folder = 'browser-choice-' + self.channel if self.channel else 'password-form'
        if self.native:
            folder += '-native'
        path = Path('browser-acceptance') / folder / 'password-form-results.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        report = self.report(success)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(report, ensure_ascii=False))

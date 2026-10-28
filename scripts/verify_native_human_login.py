"""Controlled installed-browser frame protocol check; ZERO publisher HTTP.

Fetch responses and robots policy are synthetic test fixtures. This exercises
the real owned CDP frame lifecycle and request checks, not native TLS, a real
CAPTCHA, delivery of an SMS, or a real authenticated Liepin session.
"""
import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.native_browser import NativeBackend
from vibe_job_radar.guided.native_documents import document_response_params
from vibe_job_radar.guided.native_login_frames import document_frame_source
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.login_surface import MANUAL_LOGIN_CODES

MAIN = 'https://www.liepin.com/login'
FRAME = 'https://turing.captcha.qcloud.com/template/drag_ele.html'
HTML = '''<!doctype html><body><h1>LOCAL FIXTURE ONLY</h1>
<iframe src="''' + FRAME + '''"></iframe>
<script>addEventListener('message', e => {if(e.origin==='https://turing.captcha.qcloud.com')
 document.body.dataset.child=e.data;});</script></body>'''
CHILD = '''<!doctype html><body>LOCAL VERIFICATION FIXTURE
<script>fetch('/cap_union_new_verify',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'fixture=1'})
.then(r=>r.json()).then(()=>parent.postMessage('owned-child-ready','https://www.liepin.com'));</script></body>'''


class ControlledBackend(NativeBackend):
    def __init__(self, *a, **kw):
        self.fixture_requests = {}
        self.fixture_events = []
        self.delivered = []
        super().__init__(*a, **kw)

    def _load_robots(self):
        for host in self.contract.hosts:
            self.wire.install_robots('https://' + host, 200, 'text/plain', b'User-agent: *\nAllow: /\n')

    def _received(self, event):
        message = json.loads(event['message'])
        method, data = message.get('method'), message.get('params', {})
        if method in {'Page.frameAttached', 'Page.frameDetached', 'Target.attachedToTarget'}:
            self.fixture_events.append({'method': method, 'session': event['sessionId'],
                'frame': data.get('frameId'), 'parent': data.get('parentFrameId'),
                'target': data.get('targetInfo', {}).get('targetId'),
                'type': data.get('targetInfo', {}).get('type')})
        super()._received(event)

    def _paused(self, session, event):
        if 'responseStatusCode' not in event:
            self.fixture_requests[session, event['requestId']] = event
            self.fixture_events.append({'method':'Fetch.requestPaused', 'session':session,
                'frame':event.get('frameId'), 'path':urlsplit(event['request']['url']).path})
        super()._paused(session, event)

    def _send(self, session, method, params=None, callback=None):
        if method == 'Fetch.continueRequest':
            event = self.fixture_requests.pop((session, params['requestId']))
            url = event['request']['url']
            if url == MAIN:
                content, mime = HTML, 'text/html'
            elif url == FRAME:
                content, mime = CHILD, 'text/html'
            elif urlsplit(url).path == '/cap_union_new_verify':
                content, mime = '{"fixture":true}', 'application/json'
            elif urlsplit(url).path == '/favicon.ico':
                content, mime = '', 'image/x-icon'
            else:
                raise AssertionError('unexpected fixture request: ' + url)
            event = {**event, 'responseStatusCode':200, 'responseStatusText':'OK',
                'responseHeaders':[{'name':'Content-Type','value':mime}]}
            response = document_response_params(event, enabled=True,
                login_frame=bool(document_frame_source(self, session, event)))
            response.setdefault('responseCode', 200)
            response.setdefault('responseHeaders', event['responseHeaders'])
            response['body'] = base64.b64encode(content.encode()).decode()
            self.delivered.append(urlsplit(url).path)
            # Test-only response injection: never continue ANY fixture request
            # to the tunnel, even if an assertion or browser action fails.
            return super()._send(session, 'Fetch.fulfillRequest', response, callback)
        return super()._send(session, method, params, callback)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controlled', action='store_true', required=True)
    parser.add_argument('--channel', choices=['msedge', 'chrome'], default='msedge')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'controlled_only':True, 'real_login_verified':False}
    backend = None
    try:
        with tempfile.TemporaryDirectory() as folder:
            ledger = RateLedger(Path(folder)/'rates.sqlite', Limits(page_interval=0, request_interval=0))
            backend = ControlledBackend(builtins().get('liepin'), ledger, threading.Event(),
                headless=True, channel=args.channel, human_login=True)
            result['browser_version'] = backend.browser.version
            try:
                backend.open(MAIN, authentication=True)
            except CrawlError as error:
                if error.code not in MANUAL_LOGIN_CODES:
                    raise
                result['login_gate'] = error.code
            until = time.monotonic() + 12
            while time.monotonic() < until and not backend.error:
                backend.pump()
                if backend.page.evaluate('document.body.dataset.child') == 'owned-child-ready':
                    result['owned_child_ready'] = True
                    break
            result['backend_error'] = backend.error
            result['events'] = backend.fixture_events
            result['delivered'] = backend.delivered
            result['live_child_sessions'] = len(backend._login_frames.owners)
            assert result.get('owned_child_ready') and not backend.error, result
    except Exception as error:
        result['exception'] = type(error).__name__
        result['code'] = getattr(error, 'code', None)
    finally:
        if backend:
            result.setdefault('backend_error', backend.error)
            result.setdefault('events', backend.fixture_events)
            result.setdefault('delivered', backend.delivered)
            try:
                backend.close()
                result['closed'] = True
            except Exception as error:
                result['close_error'] = type(error).__name__
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('owned_child_ready') and not result.get('exception') and result.get('closed') else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Controlled rejected-login return; no publisher egress or real account."""
import argparse
import json
from pathlib import Path
import tempfile
import threading
import time

import verify_native_login_landing as fixture
from vibe_job_radar.guided.contracts import PageSnapshotChanged


class RejectedLandingBackend(fixture.LandingBackend):
    login_html = '''<!doctype html><meta charset="utf-8"><body>
<h1>LOCAL FAILURE FIXTURE ONLY</h1><input data-nick="login-smscode">
<p id="result"></p><button id="submit">Synthetic duplicate</button>
<script>document.querySelector('#submit').onclick=async()=>{
 const headers={'Content-Type':'application/json','X-Client-Type':'web'};
 for(let n=0;n<2;n++) await fetch('''+json.dumps(fixture.LOGIN)+''',
 {method:'POST',headers,body:'{}'}).then(r=>r.json());
 document.querySelector('#result').textContent='验证码错误或过期';
 setTimeout(()=>location.assign('''+json.dumps(fixture.ROOT)+'''),1200);
};</script></body>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--controlled', action='store_true', required=True)
    parser.add_argument('--channel', choices=['msedge', 'chrome'], default='msedge')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'controlled_only': True, 'real_login_verified': False}
    with tempfile.TemporaryDirectory() as folder:
        backend = RejectedLandingBackend(fixture.builtins().get('liepin'),
            fixture.RateLedger(Path(folder)/'rates.sqlite',
                               fixture.Limits(page_interval=0, request_interval=0)),
            threading.Event(), headless=True, channel=args.channel, human_login=True)
        try:
            backend.start_manual_login()
            try:
                backend.open(fixture.MAIN, authentication=True)
            except fixture.CrawlError as exc:
                if exc.code not in fixture.MANUAL_LOGIN_CODES:
                    raise
            backend.page.locator('#submit').click(timeout=5000)
            stages = []
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                backend.page.wait_for_timeout(50)
                if backend.error:
                    break
                try:
                    progress = backend.manual_login_status()
                except PageSnapshotChanged:
                    if backend.error:
                        break  # Retain and fail on the old browser-error behavior below.
                    raise
                if not stages or stages[-1] != progress['stage']:
                    stages.append(progress['stage'])
            backend.page.wait_for_timeout(250)
            result.update(browser_version=backend.browser.version, stages=stages,
                login_submissions=backend._manual_login_progress.submits,
                main_document_preserved=backend.page.url == fixture.MAIN,
                return_ticket=fixture.peek(backend) is not None,
                account_documents_delivered=backend.delivered.count(fixture.ROOT),
                backend_error=backend.error)
            assert 'code_rejected' in stages and result['login_submissions'] == 2
            assert result['main_document_preserved'] and not result['return_ticket']
            assert not result['account_documents_delivered']
            assert backend.error == 'login_return_unverified'
            result['rejected_return_preserved'] = True
        except Exception as exc:
            result.update(exception=type(exc).__name__, error_code=getattr(exc, 'code', None))
        finally:
            result.update(trace=backend._diagnostics.snapshot(),
                          manual_login=backend._manual_login_progress.snapshot())
            try:
                backend.close()
                result['closed'] = True
            except Exception as exc:
                result['close_error'] = type(exc).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'trace'}, ensure_ascii=False))
    return 0 if result.get('rejected_return_preserved') and result.get('closed') else 1


if __name__ == '__main__':
    raise SystemExit(main())

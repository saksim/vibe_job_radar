"""Bounded offline comparison with an explicitly supplied, unchanged public script.

No account operations or live website requests. Run from the same desktop user
context as the app: an executor sandbox can prevent Chromium child startup.
The rejecting proxy remains in place in both modes; browser security flags and
automation identity are not changed to obtain a passing result.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socketserver
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from vibe_job_radar.guided.cdp_browser import CDPBrowser, edge_executable

SCRIPT_SHA256 = 'daac45187474079984300055ec0aca3a7b11e1199fab1df5788883ce6ba589de'
ENTRY = 'https://www.liepin.com/zhaopin/'
SCRIPT = 'https://concat.lietou-static.com/fe-lib-pc/v6/security/1.0.0/security.min.js'
CONFIG = 'https://dalisi4api.tongdao.cn/static/cfg/v2.json'


class Reject(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.denied += 1
        self.request.close()


@contextmanager
def rejected_network():
    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Reject) as proxy:
        proxy.daemon_threads, proxy.denied = True, 0
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield proxy
        finally:
            proxy.shutdown()
            thread.join(timeout=2)


def run(args, source):
    result = {'scope': 'offline fixture, no account or live-site acceptance',
        'controller': args.controller, 'headed': args.headed,
        'script_sha256': hashlib.sha256(source).hexdigest(),
        'script_present': not args.without_script, 'observation_seconds': 60,
        'stage': 'launch', 'requests': {}, 'navigation': [], 'success': False}
    html = ('<!doctype html><title>OFFLINE COMPATIBILITY TEST</title>'
        '<h1>OFFLINE TEST ONLY</h1><p id="fixture">No account, SMS or external network.</p>'
        + ('' if args.without_script else '<script src="' + SCRIPT + '"></script>')).encode()
    browser = runtime = None
    start = time.monotonic()
    with rejected_network() as proxy:
        try:
            options = dict(headless=not args.headed,
                args=['--disable-background-networking', '--disable-quic', '--disable-sync',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
                      '--proxy-bypass-list=<-loopback>', '--block-new-web-contents'],
                proxy={'server': 'http://127.0.0.1:' + str(proxy.server_address[1])})
            if args.controller == 'minimal_cdp':
                browser = CDPBrowser(executable_path=edge_executable(), **options)
            else:
                from playwright.sync_api import sync_playwright
                runtime = sync_playwright().start()
                browser = runtime.chromium.launch(channel='msedge', **options)
            result['browser_version'] = browser.version
            result['stage'] = 'page'
            context = browser.new_context(service_workers='block', accept_downloads=False)
            if args.controller == 'playwright':
                context.route_web_socket('**/*', lambda ws: ws.close())
            page = context.new_page()
            client = page.client if args.controller == 'minimal_cdp' else context.new_cdp_session(page)

            def navigation(event):
                if len(result['navigation']) >= 20:
                    return
                result['navigation'].append({'blank': event.get('url') == 'about:blank',
                    'seconds': round(time.monotonic() - start, 3),
                    'reason': event.get('reason') if event.get('reason') in {
                        'scriptInitiated', 'httpHeaderRefresh', 'metaTagRefresh'} else 'other'})

            def paused(event):
                request, ident = event['request'], event['requestId']
                url = request['url']
                kind = {ENTRY: 'fixture', SCRIPT: 'unchanged_script', CONFIG: 'config_failure'}.get(url, 'denied')
                counts = result['requests']
                counts[kind] = counts.get(kind, 0) + 1
                if request['method'] == 'GET' and url in {ENTRY, SCRIPT}:
                    body, mime = (html, 'text/html') if url == ENTRY else (source, 'text/javascript')
                    client.send('Fetch.fulfillRequest', {'requestId': ident, 'responseCode': 200,
                        'responseHeaders': [{'name': 'Content-Type', 'value': mime}],
                        'body': base64.b64encode(body).decode()})
                else:
                    client.send('Fetch.failRequest', {'requestId': ident,
                        'errorReason': 'NameNotResolved' if url == CONFIG else 'BlockedByClient'})

            client.on('Page.frameRequestedNavigation', navigation)
            client.on('Fetch.requestPaused', paused)
            client.send('Page.enable')
            client.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
            result['stage'] = 'navigate'
            page.goto(ENTRY, wait_until='domcontentloaded', timeout=15000)
            result['stage'] = 'observe'
            observed = time.monotonic()
            deadline = observed + 60
            while time.monotonic() < deadline and page.url != 'about:blank':
                page.wait_for_timeout(250)
            result['observed_seconds'] = round(time.monotonic() - observed, 3)
            result['blank'] = page.url == 'about:blank'
            if not result['blank']:
                result['fixture_visible'] = page.evaluate("document.querySelector('#fixture').textContent") == 'No account, SMS or external network.'
                result['script_loaded'] = page.evaluate("typeof window.LpSecurityCollect === 'object'")
                result['automation_marker_retained'] = page.evaluate('navigator.webdriver') is True
            result['success'] = (not result['blank'] and result.get('fixture_visible') is True
                and result.get('automation_marker_retained') is True
                and (args.without_script or result.get('script_loaded') is True))
        except Exception as exc:
            result['failure_type'] = type(exc).__name__
            # No raw exception, request URL, script text, or protocol payload.
        finally:
            if browser:
                try:
                    browser.close()
                    result['cleanup_ok'] = (not browser.profile.exists() and not browser.cleanup_failed
                        and browser.process.poll() is not None) if args.controller == 'minimal_cdp' else not browser.is_connected()
                except Exception:
                    result['cleanup_ok'] = False
            if runtime:
                runtime.stop()
            result['denied_proxy_connections'] = proxy.denied
    result['success'] = result['success'] and result.get('cleanup_ok') is True
    result['elapsed_seconds'] = round(time.monotonic() - start, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--security-script', type=Path, required=True)
    parser.add_argument('--controller', choices=('minimal_cdp', 'playwright'), required=True)
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--without-script', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output exists; preserve the first result and choose a new filename')
    source = args.security_script.read_bytes()
    if hashlib.sha256(source).hexdigest() != SCRIPT_SHA256:
        parser.error('public script hash differs from the pinned reproduction input')
    # Reserve the evidence destination before launching; never rerun for a typo.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        result = run(args, source)
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

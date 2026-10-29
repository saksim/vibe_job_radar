"""Explicit artificial-source comparison using Playwright's public page CDP API.

No recruiting requests; no production behavior changes. Reuses the controlled
suite's ephemeral trust and maps just its test host inside this test process.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import tempfile
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

import run_native_browser_acceptance as fixture
from native_fixture_network import mapped_native_connect
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.network_policy import NetworkPolicy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controlled', action='store_true')
    parser.add_argument('--channel', choices=['msedge'])
    args = parser.parse_args()
    if not args.controlled:
        print('No requests. Explicit --controlled is required.')
        return
    from playwright.sync_api import sync_playwright
    result = {'scope':'Artificial local test only, public page CDP API.', 'success':False,
              'request_events':0, 'proxy_challenges':0, 'server_challenges':0}
    out = fixture.ROOT / 'browser-acceptance' / 'native'
    out.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with fixture.trust_fixture(root):
                source = fixture.Fixture(root, 'good.pem')
                guard = NativeTunnel((fixture.HOST,), NetworkPolicy(), threading.Event())
                real_dns, real_dial = socket.getaddrinfo, socket.create_connection
                def dns(host, *a, **kw):
                    if host == fixture.HOST:
                        return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
                    if host in {'localhost','127.0.0.1','::1'}:
                        return real_dns(host,*a,**kw)
                    raise AssertionError('external DNS attempted by controlled comparison')
                def dial(address,*a,**kw):
                    if address == ('93.184.216.34',443):
                        return real_dial(source.server.server_address,*a,**kw)
                    if address[0] in {'localhost','127.0.0.1','::1'}:
                        return real_dial(address,*a,**kw)
                    raise AssertionError('external connection attempted by controlled comparison')
                try:
                    with patch('socket.getaddrinfo',side_effect=dns), patch('socket.create_connection',side_effect=dial), mapped_native_connect(lambda: source.server.server_address), sync_playwright() as p:
                        browser = p.chromium.launch(headless=True, channel=args.channel,
                            proxy={'server':guard.endpoint})
                        try:
                            context = browser.new_context(service_workers='block')
                            page = context.new_page()
                            cdp = context.new_cdp_session(page)
                            attempts = set()
                            def paused(event):
                                result['request_events'] += 1
                                request = event['request']
                                if request['url'] != fixture.URL+'/robots.txt' or request['method'] != 'GET':
                                    cdp.send('Fetch.failRequest',{'requestId':event['requestId'],'errorReason':'BlockedByClient'})
                                    return
                                cdp.send('Fetch.continueRequest',{'requestId':event['requestId']})
                            def auth(event):
                                challenge = event['authChallenge']
                                source_type = challenge.get('source')
                                if source_type == 'Proxy': result['proxy_challenges'] += 1
                                elif source_type == 'Server': result['server_challenges'] += 1
                                origin, expected = urlsplit(challenge.get('origin','')), urlsplit(guard.endpoint)
                                allowed = source_type == 'Proxy' and origin.hostname == expected.hostname and origin.port == expected.port and event['requestId'] not in attempts
                                answer = {'response':'CancelAuth'}
                                if allowed:
                                    attempts.add(event['requestId'])
                                    answer = {'response':'ProvideCredentials','username':guard.username,'password':guard.password}
                                cdp.send('Fetch.continueWithAuth',{'requestId':event['requestId'],'authChallengeResponse':answer})
                            cdp.on('Fetch.requestPaused',paused)
                            cdp.on('Fetch.authRequired',auth)
                            cdp.send('Fetch.enable',{'patterns':[{'urlPattern':'*'}],'handleAuthRequests':True})
                            response = page.goto(fixture.URL+'/robots.txt', timeout=20000)
                            result['success'] = bool(response and response.status==200 and result['request_events'] and result['proxy_challenges'])
                            result['source_requests'] = len(source.requests)
                            result['credentials_not_forwarded'] = all(x['proxy_secret_absent'] for x in source.requests)
                        finally:
                            browser.close()
                finally:
                    guard.close()
                    source.close()
    except Exception as exc:
        # Fixed failure classification only. Never include exception text.
        result['error_type'] = type(exc).__name__
        text = str(exc)
        result['auth_failed'] = 'ERR_INVALID_AUTH_CREDENTIALS' in text
        result['certificate_failed'] = 'ERR_CERT_' in text
    finally:
        (out/'public-page-session.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result))
    if not result['success']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

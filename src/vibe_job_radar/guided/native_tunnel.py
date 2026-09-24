"""Authenticated loopback CONNECT guard, not an HTTP response bridge.

Chromium sends/verifies TLS end-to-end. This guard sees only CONNECT authority
and opaque bytes. Reuse the workspace's existing verified public resolver and
selected HTTP/SOCKS route. No TLS interception, certificate install or fallback.
"""
from __future__ import annotations

import base64
import hmac
import re
import secrets
import select
import socket
import socketserver
import threading
import time

from ..network import FetchError, validate_public_url, _connection_candidates
from ..loopback_proxy import LocalProxyError


class NativeTunnel:
    def __init__(self, hosts, policy, cancelled, *, timeout=20):
        self.hosts = frozenset(hosts)
        self.policy, self.cancelled, self.timeout = policy, cancelled, timeout
        self.username, self.password = 'radar', secrets.token_urlsafe(32)
        self._authorization = 'Basic ' + base64.b64encode((self.username + ':' + self.password).encode()).decode()
        self._lock, self._stop = threading.RLock(), threading.Event()
        self.policy.bind_cancellation(self.cancelled).bind_cancellation(self._stop)
        self._proxy_auth_lock = threading.Lock()
        self._proxy_auth_error = None
        self._sockets = set()
        self._closed = False
        self._slots = threading.BoundedSemaphore(16)
        self.last_error = ''
        self.connections = 0
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request)
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False
            def handle_error(self, *_):
                pass  # Never print request headers or credential-bearing errors.
            def process_request(self, request, address):
                if not owner._slots.acquire(blocking=False):
                    request.close(); return
                try:
                    super().process_request(request, address)
                except Exception:
                    owner._slots.release(); raise
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    owner._slots.release()
        self.server = Server(('127.0.0.1', 0), Handler)
        self.endpoint = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .05}, daemon=True, name='radar-native-connect')
        self.thread.start()

    def _open(self, host):
        try:
            _, addresses, _ = validate_public_url('https://' + host + '/', set(self.hosts),
                all_addresses=True, network_policy=self.policy, cancelled=self.cancelled)
            proxy = self.policy.for_host(host)
            deadline = time.monotonic() + self.timeout
            ips = _connection_candidates(addresses)
            for i, ip in enumerate(ips):
                if self._stop.is_set() or self.cancelled.is_set():
                    raise FetchError('paused')
                budget = (deadline - time.monotonic()) / (len(ips) - i)
                if budget <= 0:
                    raise TimeoutError()
                try:
                    # Target IP was validated before dialing and never re-resolved.
                    self.policy.ensure_active()
                    return self._open_proxy(proxy, ip, budget) if proxy else socket.create_connection((ip,443), budget)
                except LocalProxyError as exc:
                    raise FetchError(exc.code) from exc
                except OSError as exc:
                    if getattr(exc, 'errno', None) in (1,13) or i == len(ips)-1:
                        raise
            raise TimeoutError()
        except LocalProxyError as exc:
            raise FetchError(exc.code) from exc

    def _open_proxy(self, proxy, ip, budget):
        if proxy.credentials is None:
            return proxy.open_tunnel(ip, budget)
        # Chromium may open several CONNECTs concurrently. Serialize an
        # authenticated handshake so a refused credential is never replayed by
        # another pending connection from the same owned browser session.
        deadline = time.monotonic() + budget
        if not self._proxy_auth_lock.acquire(timeout=budget):
            raise TimeoutError('proxy authentication deadline')
        try:
            if self._proxy_auth_error:
                raise LocalProxyError(self._proxy_auth_error)
            remaining = deadline - time.monotonic()
            if self._stop.is_set() or self.cancelled.is_set():
                raise FetchError('paused')
            if remaining <= 0:
                raise TimeoutError()
            try:
                return proxy.open_tunnel(ip, remaining)
            except LocalProxyError as exc:
                # A truncated/invalid reply can mean credentials were sent but
                # their result is unknown. Do not replay that handshake either.
                self._proxy_auth_error = exc.code
                raise
        finally:
            self._proxy_auth_lock.release()

    @staticmethod
    def _reply(sock, status):
        challenge = 'Proxy-Authenticate: Basic realm="Radar local native session"\r\n' if status == 407 else ''
        sock.sendall((f'HTTP/1.1 {status} Connection\r\n{challenge}Content-Length: 0\r\nConnection: close\r\n\r\n').encode())

    def _handle(self, client):
        upstream = None
        with self._lock:
            self._sockets.add(client)
        try:
            client.settimeout(3)
            raw = bytearray()
            deadline = time.monotonic()+3
            while not raw.endswith(b'\r\n\r\n'):
                if len(raw) >= 8192 or time.monotonic() > deadline:
                    self._reply(client,400); return
                data = client.recv(1)
                if not data:
                    return
                raw.extend(data)
            lines = bytes(raw).decode('ascii').split('\r\n')
            m = re.fullmatch(r'CONNECT ([a-zA-Z0-9.-]+):443 HTTP/1\.[01]', lines[0])
            headers = {}
            for line in lines[1:-2]:
                if ':' not in line or line[:1].isspace():
                    self._reply(client,400); return
                name, value = line.split(':',1); name=name.lower()
                if name in headers:
                    self._reply(client,400); return
                headers[name] = value.strip()
            if not hmac.compare_digest(headers.get('proxy-authorization',''),self._authorization):
                self._reply(client,407); return
            if (not m or m[1].lower() not in self.hosts or headers.get('transfer-encoding')
                    or headers.get('content-length','0') != '0'):
                self._reply(client,403); return
            if self._stop.is_set() or self.cancelled.is_set():
                self._reply(client,503); return
            upstream = self._open(m[1].lower())
            with self._lock:
                if self._stop.is_set() or self.cancelled.is_set():
                    return
                self._sockets.add(upstream); self.connections += 1
            client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            client.settimeout(2); upstream.settimeout(2)
            peers = {client:upstream, upstream:client}
            idle = time.monotonic()
            while not self._stop.is_set() and not self.cancelled.is_set():
                readable, _, _ = select.select(list(peers), [], [], .1)
                if not readable:
                    if time.monotonic()-idle > 60:
                        break
                    continue
                for src in readable:
                    data = src.recv(65536)
                    if not data:
                        return
                    peers[src].sendall(data)
                    idle = time.monotonic()
        except FetchError as exc:
            self.last_error = exc.code
            try: self._reply(client,502)
            except OSError: pass
        except (OSError, ValueError, UnicodeError):
            self.last_error = self.last_error or 'network_error'
        finally:
            for sock in (upstream,client):
                if sock is not None:
                    with self._lock:
                        self._sockets.discard(sock)
                    try: sock.close()
                    except OSError: pass

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        with self._lock:
            sockets = tuple(self._sockets)
        for sock in sockets:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            try: sock.close()
            except OSError: pass
        self.server.shutdown(); self.server.server_close()
        self.thread.join(timeout=2)

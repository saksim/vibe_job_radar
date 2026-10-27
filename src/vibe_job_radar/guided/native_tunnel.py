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
        try:
            policy.require_native_support()
        except LocalProxyError as exc:
            raise FetchError(exc.code) from exc
        self.hosts = frozenset(hosts)
        self.policy, self.cancelled, self.timeout = policy, cancelled, timeout
        self.username, self.password = 'radar', secrets.token_urlsafe(32)
        self._authorization = 'Basic ' + base64.b64encode((self.username + ':' + self.password).encode()).decode()
        self._lock, self._stop = threading.RLock(), threading.Event()
        self.policy.bind_cancellation(self.cancelled).bind_cancellation(self._stop)
        self._proxy_auth_lock = threading.Lock()
        self._proxy_auth_error = None
        self._sockets = set()
        self._pending_requests = set()
        self._requests_done = threading.Condition(self._lock)
        self._close_complete = threading.Event()
        self._closed = False
        self._slots = threading.BoundedSemaphore(16)
        self.last_error = ''
        self.connections = 0
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner._handle(self.request)
        class Server(socketserver.ThreadingTCPServer):
            # Buffer a bounded browser connection burst while accept is being
            # scheduled. The separate semaphore still caps active handlers.
            request_queue_size = 16
            daemon_threads = True
            block_on_close = False
            def handle_error(self, *_):
                pass  # Never print request headers or credential-bearing errors.
            def process_request(self, request, address):
                # Own an accepted socket before its worker can be descheduled.
                # Admission and close's socket snapshot share this same lock.
                with owner._lock:
                    if (owner._closed or owner._stop.is_set()
                            or not owner._slots.acquire(blocking=False)):
                        request.close(); return
                    owner._sockets.add(request)
                    owner._pending_requests.add(request)
                try:
                    super().process_request(request, address)
                except Exception:
                    try:
                        request.close()
                    except OSError:
                        pass
                    try:
                        owner._finish_request(request)
                    except Exception:
                        pass  # Keep the startup error; unfinished cleanup stays pending.
                    raise
            def process_request_thread(self, request, address):
                try:
                    super().process_request_thread(request, address)
                finally:
                    owner._finish_request(request)
        self.server = Server(('127.0.0.1', 0), Handler)
        self.endpoint = f'http://127.0.0.1:{self.server.server_address[1]}'
        try:
            self.thread = threading.Thread(target=self.server.serve_forever,
                kwargs={'poll_interval': .05}, daemon=True, name='radar-native-connect')
            self.thread.start()
        except Exception:
            # The caller cannot retain a constructor that failed. The listener
            # is already bound, so release it here; shutdown() would wait for a
            # serve_forever loop that did not start. Retain the startup failure.
            self._stop.set()
            try:
                self.server.server_close()
            except OSError:
                pass
            raise

    def resolve_host(self, host):
        """Use the session's same public resolver without opening a target socket."""
        if self._stop.is_set() or self.cancelled.is_set():
            raise FetchError('paused')
        _, addresses, _ = validate_public_url('https://' + host + '/', set(self.hosts),
            all_addresses=True, network_policy=self.policy, cancelled=self.cancelled)
        return addresses

    def _open(self, host):
        try:
            addresses = self.resolve_host(host)
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
        try:
            with self._lock:
                if self._closed or self._stop.is_set():
                    return
                # Retain direct-call compatibility; real accepted sockets were
                # already registered before their worker was started.
                self._sockets.add(client)
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
            # Browser background traffic has no owned Fetch auth handler.
            # Reject requests we can never tunnel before issuing a challenge:
            # credentials cannot make an unsupported method/authority valid.
            if (not m or m[1].lower() not in self.hosts or headers.get('transfer-encoding')
                    or headers.get('content-length','0') != '0'):
                self._reply(client,403); return
            if not hmac.compare_digest(headers.get('proxy-authorization',''),self._authorization):
                self._reply(client,407); return
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
                    # Keep the socket owned while close is in progress; a
                    # concurrent guard close must still include it.
                    try: sock.close()
                    except OSError: pass
                    with self._lock:
                        self._sockets.discard(sock)

    def _finish_request(self, request):
        # Called after socketserver has closed the accepted request, or after
        # startup failure has closed it. Completion includes the handler slot.
        with self._requests_done:
            self._slots.release()
            self._sockets.discard(request)
            self._pending_requests.discard(request)
            self._requests_done.notify_all()

    def close(self):
        with self._lock:
            repeated = self._closed
            self._closed = True
        if repeated:
            if not self._close_complete.wait(2):
                raise RuntimeError('native_tunnel_cleanup_incomplete')
            with self._lock:
                pending = bool(self._pending_requests)
            if pending or self.thread.is_alive():
                raise RuntimeError('native_tunnel_cleanup_incomplete')
            return
        self._stop.set()
        try:
            with self._lock:
                sockets = tuple(self._sockets)
            for sock in sockets:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                try: sock.close()
                except OSError: pass
            errors = []
            # Attempt each owned resource; an earlier cleanup error remains
            # primary even when a handler also fails to finish in time.
            for operation in (self.server.shutdown, self.server.server_close):
                try:
                    operation()
                except Exception as error:
                    errors.append(error)
            deadline = time.monotonic() + 2
            try:
                self.thread.join(timeout=2)
            except Exception as error:
                errors.append(error)
            # Reuse the original join budget, not another timeout per worker.
            # An in-flight open may outlive closed client/listener sockets.
            with self._requests_done:
                while self._pending_requests:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._requests_done.wait(remaining)
                pending = bool(self._pending_requests)
            if pending or self.thread.is_alive():
                errors.append(RuntimeError('native_tunnel_cleanup_incomplete'))
            if errors:
                raise errors[0]
        finally:
            self._close_complete.set()

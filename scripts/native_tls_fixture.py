"""Concurrent TLS handshakes for developer-only, loopback HTTP fixtures."""
from http.server import ThreadingHTTPServer


class NativeTLSFixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, context):
        self.tls_context = context
        super().__init__(address, handler)

    def process_request_thread(self, request, client_address):
        # Chromium can preconnect without sending ClientHello. TLS must run in
        # this connection's worker, never in the shared accept loop. Preserve
        # the existing finite socket timeout and actual certificate checks.
        request.settimeout(5)
        try:
            wrapped = self.tls_context.wrap_socket(request, server_side=True)
        except (OSError, ValueError):
            self.shutdown_request(request)
            return
        super().process_request_thread(wrapped, client_address)

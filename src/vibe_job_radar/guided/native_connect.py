"""Cancelable numeric TCP dial for sockets owned by one native collector."""
import errno
import ipaddress
import select
import socket
import time

from ..network import FetchError


def connect(address, timeout, source_address=None, *, stopped, own):
    # The caller has already selected/validated either a target IP or its
    # explicit proxy. Never resolve again or try a different route here.
    ip = ipaddress.ip_address(address[0])
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    deadline = time.monotonic() + timeout
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        own(sock)  # Register before connect, not after the proxy handshake.
        if stopped():
            raise FetchError('paused')
        if source_address is not None:
            sock.bind(source_address)
        sock.setblocking(False)
        code = sock.connect_ex(address)
        pending = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
                   10035, 10036, 10037}  # Winsock pending-connect values.
        while code:
            if stopped():
                raise FetchError('paused')
            if code not in pending:
                raise OSError(code, 'native connection failed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            _, writable, failed = select.select([], [sock], [sock], min(.05, remaining))
            if writable or failed:
                code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if stopped():
            raise FetchError('paused')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)
        return sock
    except BaseException as exc:
        try: sock.close()
        except OSError: pass
        if isinstance(exc, (OSError, ValueError)) and stopped():
            raise FetchError('paused') from None
        raise

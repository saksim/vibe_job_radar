"""Explicit NTLMv2 credentials in a short-lived, owned helper process."""
from __future__ import annotations
import importlib.metadata
import importlib.util
import logging
import multiprocessing
import os
import threading
import time

from .loopback_proxy import LocalProxyError

LIBRARY_VERSION = '0.12.4'
MAX_TOKEN = 8192
_SLOTS = threading.BoundedSemaphore(2)


def available():
    try:
        return (importlib.metadata.version('pyspnego') == LIBRARY_VERSION
                and importlib.util.find_spec('spnego') is not None)
    except (ImportError, OSError, ValueError):
        return False


def _worker(pipe, username, password):
    try:
        # Only the child's environment changes. No SSPI, ambient account,
        # credential file, workstation name or NTLMv1 negotiation is used.
        logging.disable(logging.CRITICAL)
        os.environ['LM_COMPAT_LEVEL'] = '5'
        os.environ['NETBIOS_COMPUTER_NAME'] = ''
        os.environ.pop('NTLM_USER_FILE', None)
        import spnego
        from spnego.exceptions import NegotiateOptions
        from spnego._ntlm_raw.messages import Authenticate
        context = spnego.client(username=username.decode('ascii'), password=password.decode('ascii'),
                                protocol='ntlm', options=NegotiateOptions.use_ntlm)
        if type(context).__name__ != 'NTLMProxy':
            raise ValueError()
        first = context.step()
        if not isinstance(first, bytes) or len(first) > MAX_TOKEN:
            raise ValueError()
        pipe.send_bytes(first)
        challenge = pipe.recv_bytes(MAX_TOKEN)
        response = context.step(challenge)
        parsed = Authenticate.unpack(response)
        if (not context.complete or len(response) > MAX_TOKEN or parsed.workstation
                or parsed.nt_challenge_response[16:20] != b'\x01\x01\x00\x00'):
            raise ValueError()
        pipe.send_bytes(response)
    except Exception:
        # Library exception messages can contain authentication material.
        try:
            pipe.send_bytes(b'ERROR')
        except OSError:
            pass
    finally:
        pipe.close()


class NtlmSession:
    """One two-step exchange; IPC, crypto and transport share the caller deadline."""
    def __init__(self, username, password, deadline):
        self._pipe = self._process = self._writer = None
        self._closed = False
        self._responded = False
        self._slot = False
        self._deadline = deadline
        if not available():
            raise LocalProxyError('local_proxy_ntlm_unavailable')
        if not _SLOTS.acquire(blocking=False):
            raise LocalProxyError('local_proxy_ntlm_busy')
        self._slot = True
        child = None
        try:
            self.remaining()
            context = multiprocessing.get_context('spawn')
            self._pipe, child = context.Pipe(duplex=True)
            self._process = context.Process(target=_worker, args=(child, username, password), daemon=True)
            self._process.start()
            child.close()
            child = None
            self.first = self._receive(1)
        except Exception as exc:
            self.close()
            if isinstance(exc, LocalProxyError):
                raise
            raise LocalProxyError('local_proxy_auth_failed') from None
        finally:
            if child is not None:
                child.close()

    def __repr__(self):
        return '<NtlmSession explicit credentials>'

    def remaining(self):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise LocalProxyError('local_proxy_ntlm_timeout')
        return remaining

    def _receive(self, expected):
        while True:
            if self._pipe.poll(min(.1, self.remaining())):
                result = self._pipe.recv_bytes(MAX_TOKEN)
                if (len(result) < 12 or result[:8] != b'NTLMSSP\0'
                        or int.from_bytes(result[8:12], 'little') != expected):
                    raise LocalProxyError('local_proxy_auth_failed')
                self.remaining()
                return result
            if not self._process.is_alive():
                raise LocalProxyError('local_proxy_auth_failed')

    def respond(self, challenge):
        if (self._closed or self._responded or not isinstance(challenge, bytes)
                or not 12 <= len(challenge) <= MAX_TOKEN or challenge[:8] != b'NTLMSSP\0'
                or int.from_bytes(challenge[8:12], 'little') != 2):
            raise LocalProxyError('local_proxy_auth_failed')
        self._responded = True
        failed = []
        def send():
            try:
                self._pipe.send_bytes(challenge)
            except Exception:
                failed.append(True)
        try:
            # A blocked pipe write must not outlive the original handshake budget.
            self._writer = threading.Thread(target=send, daemon=True, name='radar-ntlm-ipc')
            self._writer.start()
            self._writer.join(self.remaining())
            if self._writer.is_alive():
                raise LocalProxyError('local_proxy_ntlm_timeout')
            if failed:
                raise LocalProxyError('local_proxy_auth_failed')
            return self._receive(3)
        except LocalProxyError:
            raise
        except Exception:
            raise LocalProxyError('local_proxy_auth_failed') from None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._process is not None:
                if self._process.pid is not None:
                    self._process.join(.2)
                    if self._process.is_alive():
                        self._process.terminate()
                        self._process.join(1)
                    if self._process.is_alive():
                        self._process.kill()
                        self._process.join(1)
                self._process.close()
        finally:
            if self._pipe is not None:
                self._pipe.close()
            if self._writer is not None:
                self._writer.join(.2)
            if self._slot:
                self._slot = False
                _SLOTS.release()

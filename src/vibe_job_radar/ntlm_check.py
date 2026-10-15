"""Offline artificial NTLM component diagnostic; no configured account or network."""
from __future__ import annotations
import json
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time

from . import ntlm_worker
from .proxy_credentials import ProxyCredentials


def _server(pipe):
    try:
        logging.disable(logging.CRITICAL)
        os.environ['LM_COMPAT_LEVEL'] = '5'
        os.environ['NETBIOS_COMPUTER_NAME'] = ''
        import spnego
        from spnego.exceptions import NegotiateOptions, SpnegoError
        from spnego._ntlm_raw.messages import Authenticate
        with tempfile.TemporaryDirectory(prefix='radar-ntlm-component-') as temporary:
            path = Path(temporary) / 'artificial-credentials'
            path.write_text('RADAR:fixture:ArtificialOnlyPassword!42\n', encoding='utf-8')
            os.environ['NTLM_USER_FILE'] = str(path)
            context = spnego.server(protocol='ntlm', options=NegotiateOptions.use_ntlm)
            challenge = context.step(pipe.recv_bytes(ntlm_worker.MAX_TOKEN))
            if len(challenge) > ntlm_worker.MAX_TOKEN:
                raise ValueError()
            pipe.send_bytes(challenge)
            response = pipe.recv_bytes(ntlm_worker.MAX_TOKEN)
            parsed = Authenticate.unpack(response)
            if parsed.nt_challenge_response[16:20] != b'\x01\x01\x00\x00' or parsed.workstation:
                raise ValueError()
            try:
                context.step(response)
            except SpnegoError:
                pipe.send_bytes(b'REJECTED')
            else:
                if not context.complete:
                    raise ValueError()
                pipe.send_bytes(b'ACCEPTED')
    except Exception:
        try:
            pipe.send_bytes(b'ERROR')
        except OSError:
            pass
    finally:
        pipe.close()


def component_evidence():
    result = dict(schema_version=1, success=False, library='pyspnego:' + ntlm_worker.LIBRARY_VERSION,
        frozen_executable=bool(getattr(sys, 'frozen', False)), correct_accepted=False, wrong_rejected=False,
        ntlm_v2=False, workstation_omitted=False, worker_cleanup_verified=False,
        network_scope='offline-artificial-protocol-no-proxy-connection')
    if not ntlm_worker.available():
        return {**result, 'code': 'local_proxy_ntlm_unavailable'}
    context = multiprocessing.get_context('spawn')
    deadline = time.monotonic() + 25
    owned = []
    def receive(pipe):
        if not pipe.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError()
        return pipe.recv_bytes(ntlm_worker.MAX_TOKEN)
    try:
        for valid in (True, False):
            parent, child = context.Pipe(duplex=True)
            process = context.Process(target=_server, args=(child,), daemon=True)
            session = None
            try:
                process.start()
                owned.append(process.pid)
                child.close()
                session = ProxyCredentials(r'RADAR\fixture', 'ArtificialOnlyPassword!42' if valid
                                           else 'ArtificialWrongPassword!43').ntlm_session(deadline)
                owned.append(session._process.pid)
                parent.send_bytes(session.first)
                response = session.respond(receive(parent))
                parent.send_bytes(response)
                if receive(parent) != (b'ACCEPTED' if valid else b'REJECTED'):
                    raise ValueError()
                process.join(max(0, deadline - time.monotonic()))
                if process.exitcode != 0:
                    raise ValueError()
                result['correct_accepted' if valid else 'wrong_rejected'] = True
            finally:
                if session is not None:
                    session.close()
                if process.pid and process.is_alive():
                    process.terminate()
                    process.join(1)
                    if process.is_alive():
                        process.kill()
                        process.join(1)
                process.close()
                parent.close()
                child.close()
        if any(p.pid in owned for p in multiprocessing.active_children()):
            raise ValueError()
        result.update(success=True, ntlm_v2=True, workstation_omitted=True,
                      worker_cleanup_verified=True, code='verified')
    except Exception:
        result['code'] = 'local_proxy_auth_failed'
    return result


def run_cli():
    result = component_evidence()
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result['success'] else 1

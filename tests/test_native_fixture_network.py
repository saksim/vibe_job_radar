"""The native CI remap must stay local and retain real socket ownership."""
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided import native_connect

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    from native_fixture_network import mapped_native_connect, observed_native_proxy


class NativeFixtureNetworkTests(unittest.TestCase):
    def test_symbolic_target_connects_only_to_local_fixture_and_keeps_ownership(self):
        owned = []
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(2)
            dials = []
            for mapping, target in [
                    (mapped_native_connect(server.getsockname), ('93.184.216.34', 443)),
                    (observed_native_proxy(server.getsockname(), dials), server.getsockname())]:
                with mapping, patch('socket.getaddrinfo') as dns:
                    connection = native_connect.connect(target, 2,
                        stopped=threading.Event().is_set, own=owned.append)
                    with connection, server.accept()[0] as peer:
                        self.assertIs(owned[-1], connection)
                        connection.sendall(b'fixture'); peer.settimeout(2)
                        self.assertEqual(peer.recv(7), b'fixture')
                    dns.assert_not_called()
            self.assertEqual(dials, [server.getsockname()])

    def test_unmapped_address_or_nonlocal_fixture_cannot_dial(self):
        for address, destination in [
                (('1.1.1.1', 443), ('127.0.0.1', 12345)),
                (('external.test', 443), ('127.0.0.1', 12345)),
                (('93.184.216.34', 443), ('1.1.1.1', 443))]:
            with self.subTest(address=address), mapped_native_connect(lambda: destination), \
                    patch('socket.socket') as allocate, self.assertRaises(AssertionError):
                native_connect.connect(address, 2, stopped=lambda: False, own=lambda _: None)
            allocate.assert_not_called()
        dials = []
        with observed_native_proxy(('127.0.0.1', 12345), dials), patch('socket.socket') as allocate:
            for address in [('93.184.216.34', 443), ('127.0.0.1', 12346)]:
                with self.subTest(address=address), self.assertRaises(AssertionError):
                    native_connect.connect(address, 2, stopped=lambda: False, own=lambda _: None)
            allocate.assert_not_called()
        self.assertEqual(dials, [])

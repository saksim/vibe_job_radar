"""The native CI remap must stay local and retain real socket ownership."""
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.guided import native_connect

with patch.object(sys, 'path', [str(Path(__file__).resolve().parents[1] / 'scripts'), *sys.path]):
    from native_fixture_network import mapped_native_connect


class NativeFixtureNetworkTests(unittest.TestCase):
    def test_symbolic_target_connects_only_to_local_fixture_and_keeps_ownership(self):
        owned = []
        with socket.socket() as server:
            server.bind(('127.0.0.1', 0)); server.listen(1); server.settimeout(2)
            with mapped_native_connect(server.getsockname), patch('socket.getaddrinfo') as dns:
                connection = native_connect.connect(('93.184.216.34', 443), 2,
                    stopped=threading.Event().is_set, own=owned.append)
                with connection, server.accept()[0] as peer:
                    self.assertEqual(owned, [connection])
                    connection.sendall(b'fixture'); peer.settimeout(2)
                    self.assertEqual(peer.recv(7), b'fixture')
                dns.assert_not_called()

    def test_unmapped_address_or_nonlocal_fixture_cannot_dial(self):
        for address, destination in [
                (('1.1.1.1', 443), ('127.0.0.1', 12345)),
                (('external.test', 443), ('127.0.0.1', 12345)),
                (('93.184.216.34', 443), ('1.1.1.1', 443))]:
            with self.subTest(address=address), mapped_native_connect(lambda: destination), \
                    patch('socket.socket') as allocate, self.assertRaises(AssertionError):
                native_connect.connect(address, 2, stopped=lambda: False, own=lambda _: None)
            allocate.assert_not_called()
